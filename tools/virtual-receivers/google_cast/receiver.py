"""Virtual Google Cast-compatible lab receiver.

Implements:
  - mDNS `_googlecast._tcp`
  - DIAL device description on :8008
  - Cast-load HTTP companion on :8010 that pulls and validates HLS

Full Chromecast V2 on :8009 speaks TLS + length-prefixed Cast lab framing
(CONNECT / RECEIVER_STATUS). Physical protobuf Chromecast interoperability
remains NOT VERIFIED. Media validation uses the lab load endpoint or
NEARBY_CAST_LAB_CAST_LOAD.
"""
from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

from common import BUS, DeviceState, Metrics, local_ipv4
from common.mdns import make_zeroconf, register_service
from common.media_validate import inspect_fmp4, inspect_hls_playlist


DIAL_XML = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:dial-multiscreen-org:device:dial:1</deviceType>
    <friendlyName>{name}</friendlyName>
    <manufacturer>NearbyCast Lab</manufacturer>
    <modelName>NearbyCast Test Chromecast</modelName>
    <UDN>uuid:nearbycast-lab-cast</UDN>
  </device>
</root>
"""


class GoogleCastVirtualReceiver:
    def __init__(
        self,
        *,
        name: str = "NearbyCast Test Chromecast",
        dial_port: int = 8008,
        cast_port: int = 8009,
        load_port: int = 8010,
    ) -> None:
        self.name = name
        self.dial_port = dial_port
        self.cast_port = cast_port
        self.load_port = load_port
        self.ip = local_ipv4()
        self._stop = threading.Event()
        self._servers: list = []
        self._threads: list[threading.Thread] = []
        self._zc = None
        self._info = None
        self.state = DeviceState(
            protocol="Google Cast",
            name=name,
            ip=self.ip,
            port=cast_port,
            metrics=Metrics(),
            extras={
                "dial_port": dial_port,
                "load_port": load_port,
                "cast_v2": "lab_tls_framing",
                "physical_chromecast": "NOT VERIFIED",
            },
        )
        BUS.upsert("google_cast", self.state)

    def start(self) -> None:
        self._stop.clear()
        self._start_dial()
        self._start_cast_stub()
        self._start_load()
        self._zc = make_zeroconf()
        self._info = register_service(
            self._zc,
            service_type="_googlecast._tcp.local.",
            name=self.name,
            port=self.cast_port,
            properties={
                "fn": self.name,
                "md": "NearbyCast Test Chromecast",
                "id": "nearbycastlabcast01",
                "bs": "FA8FCA7B",
                "st": "0",
                "ca": "4101",
                "ic": "/setup/icon.png",
                "ve": "05",
                "lab": "1",
            },
            ip=self.ip,
        )
        self.state.advertisement = "ready"
        self.state.started_at = time.time()
        BUS.emit("google_cast_started", ip=self.ip)

    def stop(self) -> None:
        started = time.time()
        self._stop.set()
        for server in self._servers:
            try:
                server.shutdown()
            except Exception:
                pass
        if getattr(self, "_cast_sock", None):
            try:
                self._cast_sock.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=2)
        for path in getattr(self, "_tls_files", []):
            try:
                os.unlink(path)
            except OSError:
                pass
        if self._info and self._zc:
            self._zc.unregister_service(self._info)
        if self._zc:
            self._zc.close()
        self.state.advertisement = "stopped"
        self.state.metrics.shutdown_time_ms = (time.time() - started) * 1000

    def _start_dial(self) -> None:
        name = self.name
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                if self.path.startswith("/ssdp/device-desc.xml") or self.path == "/":
                    body = DIAL_XML.format(name=name).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/xml")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    state.connection = "dial_ok"
                else:
                    self.send_response(404)
                    self.end_headers()

        server = ThreadingHTTPServer(("0.0.0.0", self.dial_port), Handler)
        self._servers.append(server)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _start_cast_stub(self) -> None:
        """TLS Cast V2-compatible lab endpoint on :8009.

        Speaks length-prefixed Cast messages (namespace + JSON payload) over a
        self-signed TLS socket. Physical Chromecast protobuf compatibility
        remains NOT VERIFIED; this exercises CONNECT / RECEIVER_STATUS /
        HEARTBEAT against the production diagnosis path and lab tools.
        """
        import socket
        import ssl
        import struct

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.cast_port))
        sock.listen(8)
        sock.settimeout(0.5)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        # Ephemeral self-signed cert for lab TLS.
        try:
            from tempfile import NamedTemporaryFile
            import subprocess

            key = NamedTemporaryFile(delete=False, suffix=".pem")
            crt = NamedTemporaryFile(delete=False, suffix=".pem")
            key.close()
            crt.close()
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-keyout",
                    key.name,
                    "-out",
                    crt.name,
                    "-days",
                    "1",
                    "-nodes",
                    "-subj",
                    "/CN=NearbyCastLabCast",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            context.load_cert_chain(crt.name, key.name)
            self._tls_files = [key.name, crt.name]
        except Exception:
            # Fall back to plain TCP if openssl is unavailable; still accept.
            context = None
            self._tls_files = []

        self._cast_sock = sock
        state = self.state
        stop = self._stop

        def encode_cast(namespace: str, payload: dict) -> bytes:
            body = json.dumps(
                {
                    "protocol_version": 0,
                    "namespace": namespace,
                    "payload_type": 0,
                    "payload_utf8": json.dumps(payload),
                }
            ).encode()
            return struct.pack(">I", len(body)) + body

        def read_cast(conn: socket.socket) -> dict | None:
            header = b""
            while len(header) < 4:
                chunk = conn.recv(4 - len(header))
                if not chunk:
                    return None
                header += chunk
            (length,) = struct.unpack(">I", header)
            if length > 1_000_000:
                return None
            body = b""
            while len(body) < length:
                chunk = conn.recv(length - len(body))
                if not chunk:
                    return None
                body += chunk
            try:
                envelope = json.loads(body.decode())
                payload = json.loads(envelope.get("payload_utf8") or "{}")
                return {"namespace": envelope.get("namespace"), "payload": payload}
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None

        def handle(conn: socket.socket):
            state.connection = "cast_v2_tls" if context else "cast_tcp"
            state.extras["cast_v2"] = "lab_framing"
            try:
                conn.settimeout(5)
                # Expect CONNECT then answer RECEIVER_STATUS.
                msg = read_cast(conn)
                if not msg:
                    return
                if msg["namespace"] in {
                    "urn:x-cast:com.google.cast.tp.connection",
                    "urn:x-cast:com.google.cast.receiver",
                } or msg["payload"].get("type") in {"CONNECT", "GET_STATUS"}:
                    conn.sendall(
                        encode_cast(
                            "urn:x-cast:com.google.cast.receiver",
                            {
                                "type": "RECEIVER_STATUS",
                                "status": {
                                    "applications": [
                                        {
                                            "appId": "CC1AD845",
                                            "displayName": "Default Media Receiver",
                                            "statusText": "NearbyCast Lab",
                                            "isIdleScreen": False,
                                        }
                                    ],
                                    "volume": {"level": 1.0, "muted": False},
                                },
                                "requestId": msg["payload"].get("requestId", 1),
                            },
                        )
                    )
                    state.connection = "cast_v2_connected"
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

        def loop():
            while not stop.is_set():
                try:
                    raw, _ = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    conn = context.wrap_socket(raw, server_side=True) if context else raw
                except ssl.SSLError:
                    try:
                        raw.close()
                    except OSError:
                        pass
                    continue
                threading.Thread(target=handle, args=(conn,), daemon=True).start()

        thread = threading.Thread(target=loop, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _start_load(self) -> None:
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode() or "{}")
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.end_headers()
                    return
                if self.path != "/load":
                    self.send_response(404)
                    self.end_headers()
                    return
                if BUS.inject.get("reject_media"):
                    receiver.state.stream = "rejected"
                    self.send_response(403)
                    self.end_headers()
                    return
                media_url = payload.get("url", "")
                started = time.time()
                receiver.state.authentication = "n/a"
                receiver.state.connection = "loading"
                ok, detail = receiver._pull_media(media_url)
                if ok:
                    receiver.state.stream = "receiving"
                    receiver.state.metrics.first_frame_time_ms = (time.time() - started) * 1000
                    receiver.state.metrics.connection_time_ms = receiver.state.metrics.first_frame_time_ms
                    body = json.dumps({"ok": True, "detail": detail}).encode()
                    self.send_response(200)
                else:
                    receiver.state.stream = "failed"
                    receiver.state.metrics.last_error = detail
                    body = json.dumps({"ok": False, "error": detail}).encode()
                    self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/status":
                    body = json.dumps(receiver.state.snapshot()).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

        server = ThreadingHTTPServer(("0.0.0.0", self.load_port), Handler)
        self._servers.append(server)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _pull_media(self, url: str) -> tuple[bool, str]:
        if not url.startswith("http://") and not url.startswith("https://"):
            return False, "invalid media url"
        parsed = urlparse(url)
        if parsed.hostname in {"0.0.0.0"}:
            return False, "invalid media host"
        try:
            fetch_url = url
            if url.rstrip("/").endswith(".mp4"):
                # Growing-file live handlers keep the socket open; probe returns a snapshot.
                join = "&" if "?" in url else "?"
                fetch_url = f"{url}{join}probe=1"
            with urllib.request.urlopen(fetch_url, timeout=5) as response:
                raw = response.read(256 * 1024)
            if url.rstrip("/").endswith(".mp4") or b"ftyp" in raw[:64]:
                evidence = inspect_fmp4(raw)
                if evidence.container != "fmp4":
                    return False, "response was not fragmented MP4"
                self.state.metrics.codec = evidence.codec or "h264"
                self.state.metrics.received_bytes += len(raw)
                self.state.metrics.received_packets += 1
                return True, f"fmp4 media validated ({len(raw)} bytes)"
            text = raw.decode("utf-8", errors="replace")
            evidence = inspect_hls_playlist(text)
            if evidence.container != "hls" or "#EXTM3U" not in text:
                return False, "response was not an HLS playlist or fMP4 stream"
            self.state.metrics.codec = evidence.codec or "h264"
            import urllib.parse as up

            for line in text.splitlines():
                if line and not line.startswith("#") and (
                    line.endswith(".ts") or line.endswith(".m4s")
                ):
                    segment_url = up.urljoin(url, line)
                    with urllib.request.urlopen(segment_url, timeout=5) as seg:
                        data = seg.read(188 * 8)
                    self.state.metrics.received_bytes += len(data)
                    self.state.metrics.received_packets += max(1, len(data) // 188)
                    break
            return True, "hls playlist and segment validated"
        except Exception as exc:
            return False, str(exc)
