"""Virtual NearbyCast receiver — real TCP control + token-gated media."""
from __future__ import annotations

import json
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Optional

from common import BUS, DeviceState, Metrics, local_ipv4
from common.mdns import make_zeroconf, register_service
from common.media_validate import inspect_mpegts


class NearbyCastVirtualReceiver:
    def __init__(
        self,
        *,
        name: str = "NearbyCast Test Receiver",
        port: int = 29871,
        pairing_code: str = "123 456",
        config_dir: Optional[Path] = None,
        reject_auth: bool = False,
        reject_media: bool = False,
    ) -> None:
        self.name = name
        self.port = port
        self.pairing_code = pairing_code
        self.config_dir = config_dir or Path("/tmp/nearbycast-virtual-nearby")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.reject_auth = reject_auth
        self.reject_media = reject_media
        self.device_id = "nc-virtualreceiver01"
        self.public_key_hex = "ab" * 32
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._zc = None
        self._info = None
        self._trust: set[str] = set()
        self._pending: dict[str, dict] = {}
        self.ip = local_ipv4()
        self.state = DeviceState(
            protocol="Nearby Cast",
            name=name,
            ip=self.ip,
            port=port,
            metrics=Metrics(),
            extras={"pairing_code": pairing_code, "device_id": self.device_id},
        )
        BUS.upsert("nearbycast", self.state)

    def start(self) -> None:
        self._stop.clear()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", self.port))
        listener.listen(16)
        listener.settimeout(0.5)
        self._listener = listener
        self._zc = make_zeroconf()
        self._info = register_service(
            self._zc,
            service_type="_nearbycast._tcp.local.",
            name=self.name,
            port=self.port,
            properties={
                "id": self.device_id,
                "pk": self.public_key_hex,
                "name": self.name,
                "proto": "nearbycast",
                "ver": "1",
                "lab": "1",
            },
            ip=self.ip,
        )
        self.state.advertisement = "ready"
        self.state.started_at = time.time()
        BUS.emit("nearbycast_started", ip=self.ip, port=self.port)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        started = time.time()
        self._stop.set()
        try:
            socket.create_connection((self.ip, self.port), timeout=0.2).close()
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=2)
        if self._info and self._zc:
            self._zc.unregister_service(self._info)
        if self._zc:
            self._zc.close()
        try:
            self._listener.close()
        except Exception:
            pass
        self.state.advertisement = "stopped"
        self.state.connection = "idle"
        self.state.stream = "idle"
        self.state.metrics.shutdown_time_ms = (time.time() - started) * 1000
        BUS.emit("nearbycast_stopped")

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_client, args=(conn, addr), daemon=True
            ).start()

    def _readline(self, conn: socket.socket) -> str:
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(1)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 64 * 1024:
                raise ValueError("control line too large")
        return buf.decode("utf-8", errors="replace").strip()

    def _send(self, conn: socket.socket, payload: dict) -> None:
        conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))

    def _handle_client(self, conn: socket.socket, addr) -> None:
        started = time.time()
        self.state.connection = "connected"
        try:
            hello_line = self._readline(conn)
            try:
                hello = json.loads(hello_line)
            except json.JSONDecodeError:
                self._send(conn, {"type": "reject", "reason": "Malformed hello"})
                return
            if hello.get("type") != "hello":
                self._send(conn, {"type": "reject", "reason": "Expected hello"})
                return
            device_id = hello.get("device_id", "")
            public_key = hello.get("public_key_hex", "")
            if len(public_key) != 64:
                self._send(conn, {"type": "reject", "reason": "Invalid public key"})
                return

            inject = BUS.inject
            if inject.get("reject_auth") or self.reject_auth:
                self._send(conn, {"type": "reject", "reason": "Authentication rejected by lab"})
                self.state.authentication = "rejected"
                self.state.metrics.last_error = "auth rejected"
                return

            trusted = device_id in self._trust
            if not trusted:
                pairing_id = secrets.token_hex(16)
                self._pending[pairing_id] = {
                    "device_id": device_id,
                    "public_key": public_key,
                    "expires": time.time() + 120,
                }
                self.state.authentication = "pairing"
                self.state.extras["active_pairing_code"] = self.pairing_code
                self._send(
                    conn,
                    {
                        "type": "pair_required",
                        "pairing_id": pairing_id,
                        "code_hint": "Confirm the six-digit code shown on the receiver",
                        "expires_in_secs": 120,
                    },
                )
                submit_line = self._readline(conn)
                submit = json.loads(submit_line)
                if submit.get("type") != "pair_submit":
                    self._send(conn, {"type": "reject", "reason": "Expected pair_submit"})
                    return
                pending = self._pending.pop(submit.get("pairing_id", ""), None)
                code = "".join(ch for ch in str(submit.get("code", "")) if ch.isdigit())
                expected = "".join(ch for ch in self.pairing_code if ch.isdigit())
                if (
                    not pending
                    or pending["device_id"] != device_id
                    or pending["expires"] < time.time()
                    or code != expected
                ):
                    self._send(conn, {"type": "reject", "reason": "Pairing code rejected or expired"})
                    self.state.authentication = "rejected"
                    return
                self._trust.add(device_id)
                self.state.authentication = "trusted"
                self.state.metrics.authentication_time_ms = (time.time() - started) * 1000
            else:
                self.state.authentication = "trusted"

            if inject.get("crash"):
                conn.close()
                return

            media_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            media_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            media_listener.bind(("0.0.0.0", 0))
            media_listener.listen(1)
            media_port = media_listener.getsockname()[1]
            token = secrets.token_hex(16)
            self._send(
                conn,
                {
                    "type": "authorized",
                    "session_token": token,
                    "media_port": media_port,
                },
            )
            self.state.metrics.connection_time_ms = (time.time() - started) * 1000
            media_listener.settimeout(30)
            media_conn, _ = media_listener.accept()
            header = b""
            while b"\n" not in header:
                chunk = media_conn.recv(1)
                if not chunk:
                    break
                header += chunk
            presented = header.decode().strip().removeprefix("NEARBYCAST-SESSION ").strip()
            if presented != token or inject.get("reject_media") or self.reject_media:
                self.state.stream = "rejected"
                self.state.metrics.last_error = "media auth failed"
                media_conn.close()
                media_listener.close()
                return

            self.state.stream = "receiving"
            first = True
            t0 = time.time()
            packets = 0
            total = 0
            while not self._stop.is_set():
                data = media_conn.recv(64 * 1024)
                if not data:
                    break
                if first:
                    self.state.metrics.first_frame_time_ms = (time.time() - t0) * 1000
                    evidence = inspect_mpegts(data)
                    self.state.metrics.codec = evidence.codec
                    first = False
                packets += 1
                total += len(data)
                self.state.metrics.received_packets = packets
                self.state.metrics.received_bytes = total
                elapsed = max(0.001, time.time() - t0)
                self.state.metrics.bitrate_kbps = (total * 8 / 1000) / elapsed
                if inject.get("latency_ms"):
                    time.sleep(float(inject["latency_ms"]) / 1000.0)
            self.state.stream = "idle"
            media_conn.close()
            media_listener.close()
        except ValueError as exc:
            self.state.metrics.last_error = str(exc)
            self.state.connection = "rejected"
            try:
                self._send(conn, {"type": "reject", "reason": str(exc)})
            except OSError:
                pass
        except Exception as exc:
            self.state.metrics.last_error = str(exc)
            self.state.stream = "failed"
            BUS.emit("nearbycast_error", error=str(exc))
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self.state.connection = "idle"
