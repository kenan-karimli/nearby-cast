"""Virtual AirPlay receiver lab endpoint.

Implements a production-compatible AirPlay *lab* mirroring surface:
  - Bonjour/mDNS `_airplay._tcp` with lab=1
  - /info + /server-info capability advertisement
  - PIN pair-setup / pair-verify (lab credentials, not FairPlay)
  - RTSP OPTIONS/SETUP/RECORD mirroring handshake
  - Authenticated MPEG-TS media acceptance

Physical Apple TV / FairPlay interoperability: NOT VERIFIED.
Modern Apple devices that require FairPlay will fail pair-setup honestly.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from common import BUS, DeviceState, Metrics, local_ipv4
from common.latency import extract_timestamps, now_ns
from common.mdns import make_zeroconf, register_service
from common.media_validate import inspect_mpegts


LAB_PIN = "0000"
# Feature bit for screen mirroring in AirPlay TXT (historical bit 0x200 / display).
LAB_FEATURES = "0x5A7FFFF7"


class AirPlayVirtualReceiver:
    def __init__(
        self,
        *,
        name: str = "NearbyCast Test Apple TV",
        port: int = 7000,
        pairing_pin: str = LAB_PIN,
        inject_faults: bool | None = None,
    ) -> None:
        self.name = name
        self.port = port
        self.pairing_pin = pairing_pin.replace(" ", "")
        self.ip = local_ipv4()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._rtsp_sock: Optional[socket.socket] = None
        self._rtsp_thread: Optional[threading.Thread] = None
        self._media_sock: Optional[socket.socket] = None
        self._media_thread: Optional[threading.Thread] = None
        self._zc = None
        self._info = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        self.rtsp_port = port + 10
        self.media_port = port + 20
        self.state = DeviceState(
            protocol="AirPlay",
            name=name,
            ip=self.ip,
            port=port,
            metrics=Metrics(),
            extras={
                "pairing": "lab_pin",
                "screen_mirroring": "lab_rtsp",
                "physical_apple": "NOT VERIFIED",
                "fairplay": "NOT IMPLEMENTED",
                "implemented": [
                    "mdns",
                    "info",
                    "server-info",
                    "pair-setup",
                    "pair-verify",
                    "rtsp",
                    "mirroring_media",
                ],
                "pairing_pin": pairing_pin,
                "rtsp_port": self.rtsp_port,
                "media_port": self.media_port,
            },
        )
        BUS.upsert("airplay", self.state)

    def start(self) -> None:
        self._stop.clear()
        self._start_http()
        self._start_rtsp()
        self._start_media()
        self._zc = make_zeroconf()
        self._info = register_service(
            self._zc,
            service_type="_airplay._tcp.local.",
            name=self.name,
            port=self.port,
            properties={
                "deviceid": "AA:BB:CC:DD:EE:FF",
                "features": LAB_FEATURES,
                "flags": "0x4",
                "model": "NearbyCastLab1,1",
                "pk": "a" * 64,
                "pi": "nearbycast-lab-airplay",
                "srcvers": "366.0",
                "vv": "2",
                "lab": "1",
                "name": self.name,
                "acl": "0",
                "rsf": "0x0",
            },
            ip=self.ip,
        )
        # Explicit lab type so discovery can mark simulator_verified without
        # claiming FairPlay-capable Apple hardware.
        try:
            self._lab_info = register_service(
                self._zc,
                service_type="_airplay-lab._tcp.local.",
                name=f"{self.name} Lab",
                port=self.port,
                properties={
                    "deviceid": "AA:BB:CC:DD:EE:FF",
                    "lab": "1",
                    "name": self.name,
                    "rtsp": str(self.rtsp_port),
                    "media": str(self.media_port),
                },
                ip=self.ip,
            )
        except Exception:
            self._lab_info = None
        self.state.advertisement = "ready"
        self.state.started_at = time.time()
        BUS.emit("airplay_started", ip=self.ip, port=self.port)

    def stop(self) -> None:
        started = time.time()
        self._stop.set()
        if self._server:
            self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=2)
        for sock in (self._rtsp_sock, self._media_sock):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        for thread in (self._rtsp_thread, self._media_thread):
            if thread:
                thread.join(timeout=2)
        if getattr(self, "_lab_info", None) and self._zc:
            try:
                self._zc.unregister_service(self._lab_info)
            except Exception:
                pass
        if self._info and self._zc:
            self._zc.unregister_service(self._info)
        if self._zc:
            self._zc.close()
        self.state.advertisement = "stopped"
        self.state.metrics.shutdown_time_ms = (time.time() - started) * 1000

    def _info_payload(self) -> dict:
        return {
            "deviceID": "AA:BB:CC:DD:EE:FF",
            "name": self.name,
            "model": "NearbyCastLab1,1",
            "sourceVersion": "366.0",
            "lab": True,
            "features": LAB_FEATURES,
            "statusFlags": 4,
            "displays": [
                {
                    "widthPhysical": 0,
                    "heightPhysical": 0,
                    "widthPixels": 1920,
                    "heightPixels": 1080,
                    "refreshRate": 60.0,
                    "uuid": "nearbycast-lab-display",
                }
            ],
            "keeper": {
                "rtspPort": self.rtsp_port,
                "mediaPort": self.media_port,
                "pairing": "pin",
                "fairPlay": False,
                "mirroring": True,
            },
        }

    def _start_http(self) -> None:
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _json(self, code: int, payload: dict):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path in {"/info", "/server-info"}:
                    receiver.state.connection = "info_ok"
                    self._json(200, receiver._info_payload())
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b""
                if BUS.inject.get("airplay_refuse_pair"):
                    receiver.state.authentication = "rejected"
                    self._json(403, {"error": "pair refused by fault injection"})
                    return

                if self.path == "/pair-setup":
                    # FairPlay / Apple proprietary binary pair-setup is refused
                    # before JSON parsing — binary payloads are not lab PIN JSON.
                    content_type = (self.headers.get("Content-Type") or "").lower()
                    if "octet-stream" in content_type:
                        receiver.state.authentication = "rejected"
                        receiver.state.metrics.last_error = (
                            "FairPlay pair-setup is not implemented; physical Apple auth NOT VERIFIED"
                        )
                        self._json(
                            501,
                            {
                                "error": "fairplay_unsupported",
                                "message": "Use lab PIN pair-setup against NearbyCast AirPlay lab receivers",
                            },
                        )
                        return
                    try:
                        payload = json.loads(raw.decode() or "{}")
                    except json.JSONDecodeError:
                        self._json(400, {"error": "invalid json"})
                        return
                    if payload.get("method") == "fairplay":
                        receiver.state.authentication = "rejected"
                        receiver.state.metrics.last_error = (
                            "FairPlay pair-setup is not implemented; physical Apple auth NOT VERIFIED"
                        )
                        self._json(
                            501,
                            {
                                "error": "fairplay_unsupported",
                                "message": "Use lab PIN pair-setup against NearbyCast AirPlay lab receivers",
                            },
                        )
                        return
                    code = str(payload.get("code", "")).replace(" ", "")
                    if code != receiver.pairing_pin:
                        receiver.state.authentication = "rejected"
                        self._json(401, {"error": "invalid_pin"})
                        return
                    session_id = secrets.token_hex(16)
                    session_key = hashlib.sha256(
                        f"{session_id}:{receiver.pairing_pin}:nearbycast-airplay-lab".encode()
                    ).hexdigest()
                    with receiver._lock:
                        receiver._sessions[session_id] = {
                            "key": session_key,
                            "verified": False,
                            "created": time.time(),
                        }
                    receiver.state.authentication = "paired"
                    self._json(
                        200,
                        {
                            "ok": True,
                            "session_id": session_id,
                            "session_key": session_key,
                            "rtsp_port": receiver.rtsp_port,
                            "media_port": receiver.media_port,
                            "lab": True,
                        },
                    )
                    return

                if self.path == "/pair-verify":
                    try:
                        payload = json.loads(raw.decode() or "{}")
                    except json.JSONDecodeError:
                        self._json(400, {"error": "invalid json"})
                        return
                    session_id = str(payload.get("session_id", ""))
                    session_key = str(payload.get("session_key", ""))
                    with receiver._lock:
                        session = receiver._sessions.get(session_id)
                        if not session or session["key"] != session_key:
                            receiver.state.authentication = "rejected"
                            self._json(401, {"error": "invalid_session"})
                            return
                        session["verified"] = True
                    receiver.state.authentication = "verified"
                    self._json(200, {"ok": True, "session_id": session_id})
                    return

                if self.path in {"/fp-setup", "/fairplay"}:
                    receiver.state.authentication = "rejected"
                    self._json(501, {"error": "fairplay_unsupported"})
                    return

                self.send_response(404)
                self.end_headers()

        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _start_rtsp(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.rtsp_port))
        sock.listen(8)
        sock.settimeout(0.5)
        self._rtsp_sock = sock
        receiver = self

        def handle_client(conn: socket.socket):
            conn.settimeout(10)
            session = "1"
            try:
                while not receiver._stop.is_set():
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            return
                        data += chunk
                    header, _, rest = data.partition(b"\r\n\r\n")
                    text = header.decode("utf-8", errors="replace")
                    lines = text.split("\r\n")
                    request_line = lines[0] if lines else ""
                    headers = {}
                    for line in lines[1:]:
                        if ":" in line:
                            key, value = line.split(":", 1)
                            headers[key.strip().lower()] = value.strip()
                    length = int(headers.get("content-length", "0") or "0")
                    body = rest
                    while len(body) < length:
                        body += conn.recv(length - len(body))
                    cseq = headers.get("cseq", "1")
                    method = request_line.split(" ", 1)[0].upper() if request_line else ""

                    if BUS.inject.get("airplay_malformed_rtsp"):
                        conn.sendall(b"RTSP/1.0 500 Internal Server Error\r\nCSeq: 0\r\n\r\n")
                        return

                    if method == "OPTIONS":
                        response = (
                            f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n"
                            "Public: OPTIONS, SETUP, RECORD, TEARDOWN, GET_PARAMETER, SET_PARAMETER\r\n\r\n"
                        )
                    elif method == "SETUP":
                        auth = headers.get("authorization", "")
                        session_id = headers.get("x-apple-session-id") or headers.get(
                            "session-id", ""
                        )
                        with receiver._lock:
                            sess = receiver._sessions.get(session_id)
                            ok = bool(sess and sess.get("verified"))
                            if auth.startswith("Bearer "):
                                token = auth[7:].strip()
                                ok = ok and sess and sess.get("key") == token
                        if not ok:
                            receiver.state.authentication = "rejected"
                            response = f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n\r\n"
                        else:
                            session = session_id or "1"
                            receiver.state.connection = "rtsp_setup"
                            response = (
                                f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n"
                                f"Session: {session}\r\n"
                                f"Transport: RTP/AVP/TCP;unicast;interleaved=0-1;mode=record;"
                                f"destination={receiver.ip};server_port={receiver.media_port}\r\n\r\n"
                            )
                    elif method == "RECORD":
                        receiver.state.connection = "rtsp_record"
                        receiver.state.stream = "ready"
                        response = (
                            f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: {session}\r\n\r\n"
                        )
                    elif method == "TEARDOWN":
                        receiver.state.stream = "stopped"
                        response = f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n\r\n"
                        conn.sendall(response.encode())
                        return
                    else:
                        response = f"RTSP/1.0 501 Not Implemented\r\nCSeq: {cseq}\r\n\r\n"
                    conn.sendall(response.encode())
            except OSError:
                return
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

        def loop():
            while not receiver._stop.is_set():
                try:
                    conn, _ = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=handle_client, args=(conn,), daemon=True).start()

        self._rtsp_thread = threading.Thread(target=loop, daemon=True)
        self._rtsp_thread.start()

    def _start_media(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.media_port))
        sock.listen(4)
        sock.settimeout(0.5)
        self._media_sock = sock
        receiver = self

        def handle(conn: socket.socket):
            conn.settimeout(15)
            started = time.time()
            try:
                header = b""
                while b"\n" not in header and len(header) < 512:
                    chunk = conn.recv(1)
                    if not chunk:
                        return
                    header += chunk
                line = header.decode("utf-8", errors="replace").strip()
                if not line.startswith("AIRPLAY-SESSION "):
                    receiver.state.stream = "rejected"
                    return
                token = line.split(" ", 1)[1].strip()
                with receiver._lock:
                    authorized = any(
                        sess.get("verified") and sess.get("key") == token
                        for sess in receiver._sessions.values()
                    )
                if not authorized:
                    receiver.state.authentication = "rejected"
                    receiver.state.stream = "rejected"
                    return
                if BUS.inject.get("packet_loss"):
                    # Still accept but drop some reads after validating framing.
                    pass
                total = 0
                packets = 0
                while not receiver._stop.is_set():
                    data = conn.recv(188 * 32)
                    if not data:
                        break
                    if BUS.inject.get("disappear"):
                        receiver.state.stream = "disappeared"
                        receiver.state.metrics.last_error = "receiver disappeared (fault inject)"
                        break
                    if BUS.inject.get("packet_loss") and packets % 10 == 0:
                        receiver.state.metrics.dropped_packets += 1
                        packets += 1
                        continue
                    recv_ns = now_ns()
                    for send_ns in extract_timestamps(data):
                        receiver.state.metrics.record_latency_ms((recv_ns - send_ns) / 1_000_000.0)
                    total += len(data)
                    packets += max(1, len(data) // 188)
                    if packets == 1 or (total > 0 and receiver.state.metrics.first_frame_time_ms is None):
                        evidence = inspect_mpegts(data)
                        receiver.state.metrics.codec = evidence.codec or "h264"
                        receiver.state.metrics.first_frame_time_ms = (time.time() - started) * 1000
                    receiver.state.stream = "receiving"
                    receiver.state.metrics.received_bytes = total
                    receiver.state.metrics.received_packets = packets
                    if BUS.inject.get("latency_ms"):
                        time.sleep(float(BUS.inject["latency_ms"]) / 1000.0)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

        def loop():
            while not receiver._stop.is_set():
                try:
                    conn, _ = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=handle, args=(conn,), daemon=True).start()

        self._media_thread = threading.Thread(target=loop, daemon=True)
        self._media_thread.start()
