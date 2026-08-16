"""Virtual Miracast WFD sink: RTSP negotiation + RTP media validation (no P2P)."""
from __future__ import annotations

import random
import socket
import threading
import time
from typing import Optional

from common import BUS, DeviceState, Metrics, local_ipv4
from common.latency import extract_timestamps, now_ns
from common.mdns import make_zeroconf, register_service
from common.media_validate import inspect_rtp_mpegts


WFD_VIDEO = (
    "00 00 01 01 000001FF 02 01 01 00 0001ffff 00 00 00 00 00 00 00 00 00 00 00 00 "
    "00 00 00 00 00 00"
)
WFD_AUDIO = "AAC 00000001 00"


class MiracastWfdVirtualReceiver:
    """Wi-Fi Display sink over TCP RTSP + UDP RTP. Physical P2P is out of scope."""

    def __init__(self, *, name: str = "NearbyCast Test Display", rtsp_port: int = 7236) -> None:
        self.name = name
        self.rtsp_port = rtsp_port
        self.ip = local_ipv4()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._rtp_thread: Optional[threading.Thread] = None
        self._zc = None
        self._info = None
        self.rtp_port = 19000 + random.randint(0, 500)
        self.state = DeviceState(
            protocol="Miracast",
            name=name,
            ip=self.ip,
            port=rtsp_port,
            metrics=Metrics(),
            extras={
                "transport": "lab-rtsp",
                "physical_p2p": "NOT VERIFIED",
                "rtp_port": self.rtp_port,
            },
        )
        BUS.upsert("miracast", self.state)

    def start(self) -> None:
        self._stop.clear()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("0.0.0.0", self.rtsp_port))
        self._listener.listen(4)
        self._listener.settimeout(0.5)

        self._rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rtp.bind(("0.0.0.0", self.rtp_port))
        self._rtp.settimeout(0.5)

        self._zc = make_zeroconf()
        self._info = register_service(
            self._zc,
            service_type="_wfd-lab._tcp.local.",
            name=self.name,
            port=self.rtsp_port,
            properties={
                "fn": self.name,
                "name": self.name,
                "lab": "1",
                "proto": "miracast-wfd",
                "rtsp": str(self.rtsp_port),
                "rtp": str(self.rtp_port),
                "id": f"wfd-lab-{self.rtsp_port}",
            },
            ip=self.ip,
        )
        self.state.advertisement = "ready"
        self.state.started_at = time.time()
        self.state.extras["rtp_port"] = self.rtp_port
        BUS.emit("miracast_started", ip=self.ip, rtsp=self.rtsp_port, rtp=self.rtp_port)
        self._thread = threading.Thread(target=self._serve_rtsp, daemon=True)
        self._rtp_thread = threading.Thread(target=self._serve_rtp, daemon=True)
        self._thread.start()
        self._rtp_thread.start()

    def stop(self) -> None:
        started = time.time()
        self._stop.set()
        try:
            socket.create_connection((self.ip, self.rtsp_port), timeout=0.2).close()
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=2)
        if self._rtp_thread:
            self._rtp_thread.join(timeout=2)
        if self._info and self._zc:
            self._zc.unregister_service(self._info)
        if self._zc:
            self._zc.close()
        for sock in (getattr(self, "_listener", None), getattr(self, "_rtp", None)):
            try:
                sock.close()
            except Exception:
                pass
        self.state.advertisement = "stopped"
        self.state.metrics.shutdown_time_ms = (time.time() - started) * 1000
        BUS.emit("miracast_stopped")

    def _serve_rtp(self) -> None:
        first = True
        t0 = time.time()
        while not self._stop.is_set():
            try:
                packet, _addr = self._rtp.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if BUS.inject.get("packet_loss", 0) and random.random() < float(BUS.inject["packet_loss"]):
                self.state.metrics.dropped_packets += 1
                continue
            if BUS.inject.get("disappear"):
                self.state.stream = "disappeared"
                self.state.metrics.last_error = "receiver disappeared (fault inject)"
                break
            self.state.stream = "receiving"
            self.state.metrics.received_packets += 1
            self.state.metrics.received_bytes += len(packet)
            recv_ns = now_ns()
            for send_ns in extract_timestamps(packet[12:] if len(packet) > 12 else packet):
                self.state.metrics.record_latency_ms((recv_ns - send_ns) / 1_000_000.0)
            if first and len(packet) >= 12:
                self.state.metrics.first_frame_time_ms = (time.time() - t0) * 1000
                evidence = inspect_rtp_mpegts(packet)
                self.state.metrics.codec = evidence.codec or "h264"
                first = False
            elapsed = max(0.001, time.time() - t0)
            self.state.metrics.bitrate_kbps = (self.state.metrics.received_bytes * 8 / 1000) / elapsed
            if self.state.metrics.received_packets > 1:
                self.state.metrics.average_fps = self.state.metrics.received_packets / elapsed

    def _serve_rtsp(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_rtsp, args=(conn, addr), daemon=True).start()

    def _read_message(self, conn: socket.socket) -> Optional[str]:
        data = b""
        conn.settimeout(30)
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            data += chunk
            if len(data) > 65536:
                break
        header, _, rest = data.partition(b"\r\n\r\n")
        text = header.decode("utf-8", errors="replace")
        length = 0
        for line in text.split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip() or "0")
        body = rest
        while len(body) < length:
            body += conn.recv(length - len(body))
        return text + "\r\n\r\n" + body[:length].decode("utf-8", errors="replace")

    def _reply(self, conn: socket.socket, status: str, cseq: str, extra_headers: str = "", body: str = "") -> None:
        payload = (
            f"RTSP/1.0 {status}\r\n"
            f"CSeq: {cseq}\r\n"
            f"{extra_headers}"
            f"Content-Length: {len(body.encode())}\r\n"
            f"\r\n"
            f"{body}"
        )
        conn.sendall(payload.encode("utf-8"))

    def _handle_rtsp(self, conn: socket.socket, addr) -> None:
        started = time.time()
        self.state.connection = "connected"
        session_id = str(random.randint(1_000_000, 9_999_999))
        try:
            # WFD source typically sends OPTIONS first (M1). Sink may also receive client OPTIONS.
            while not self._stop.is_set():
                message = self._read_message(conn)
                if message is None:
                    break
                lines = message.split("\r\n")
                request_line = lines[0]
                headers = {}
                for line in lines[1:]:
                    if ":" in line:
                        key, value = line.split(":", 1)
                        headers[key.strip().lower()] = value.strip()
                cseq = headers.get("cseq", "1")
                method = request_line.split(" ", 1)[0].upper()

                if method == "OPTIONS":
                    if BUS.inject.get("miracast_malformed_rtsp"):
                        conn.sendall(b"NOT-RTSP garbage\r\n\r\n")
                        return
                    self._reply(
                        conn,
                        "200 OK",
                        cseq,
                        extra_headers="Public: org.wfa.wfd1.0, GET_PARAMETER, SET_PARAMETER, SETUP, PLAY, PAUSE, TEARDOWN\r\n",
                    )
                    # Respond as sink by advertising readiness; sources often continue with GET_PARAMETER.
                    continue

                if method == "GET_PARAMETER":
                    body = (
                        f"wfd_video_formats: {WFD_VIDEO}\r\n"
                        f"wfd_audio_codecs: {WFD_AUDIO}\r\n"
                        "wfd_content_protection: none\r\n"
                        f"wfd_client_rtp_ports: RTP/AVP/UDP;unicast {self.rtp_port} 0 mode=play\r\n"
                    )
                    self._reply(
                        conn,
                        "200 OK",
                        cseq,
                        extra_headers="Content-Type: text/parameters\r\n",
                        body=body,
                    )
                    self.state.metrics.negotiation_time_ms = (time.time() - started) * 1000
                    continue

                if method == "SET_PARAMETER":
                    if "wfd_trigger_method: SETUP" in message:
                        self._reply(conn, "200 OK", cseq)
                        continue
                    self._reply(conn, "200 OK", cseq)
                    continue

                if method == "SETUP":
                    transport = (
                        f"Transport: RTP/AVP/UDP;unicast;"
                        f"client_port={self.rtp_port}-{self.rtp_port};"
                        f"server_port={self.rtp_port}-{self.rtp_port}\r\n"
                        f"Session: {session_id}\r\n"
                    )
                    self._reply(conn, "200 OK", cseq, extra_headers=transport)
                    continue

                if method == "PLAY":
                    if BUS.inject.get("reject_media"):
                        self._reply(conn, "403 Forbidden", cseq)
                        self.state.stream = "rejected"
                        continue
                    if BUS.inject.get("close_after_setup"):
                        self._reply(conn, "200 OK", cseq, extra_headers=f"Session: {session_id}\r\n")
                        self.state.stream = "closed"
                        conn.close()
                        return
                    self._reply(conn, "200 OK", cseq, extra_headers=f"Session: {session_id}\r\n")
                    self.state.stream = "playing"
                    self.state.authentication = "n/a"
                    BUS.emit("miracast_play", peer=str(addr))
                    continue

                if method == "TEARDOWN":
                    self._reply(conn, "200 OK", cseq, extra_headers=f"Session: {session_id}\r\n")
                    self.state.stream = "idle"
                    break

                # Tolerate keepalives / unknown WFD extensions without crashing.
                self._reply(conn, "200 OK", cseq)
        except Exception as exc:
            self.state.metrics.last_error = str(exc)
            BUS.emit("miracast_error", error=str(exc))
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self.state.connection = "idle"
            self.state.metrics.connection_time_ms = (time.time() - started) * 1000
