#!/usr/bin/env python3
"""NearbyCast Virtual Receiver Lab orchestrator."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Ensure lab mode defaults for child processes / discovery helpers.
os.environ.setdefault("NEARBY_CAST_VIRTUAL_LAB", "1")
os.environ.setdefault("NEARBY_CAST_ALLOW_LOOPBACK", "1")

from airplay.receiver import AirPlayVirtualReceiver
from common import BUS, local_ipv4
from google_cast.receiver import GoogleCastVirtualReceiver
from miracast.receiver import MiracastWfdVirtualReceiver
from nearbycast.receiver import NearbyCastVirtualReceiver


class Lab:
    def __init__(self) -> None:
        self.ip = local_ipv4()
        self.nearby = NearbyCastVirtualReceiver(port=29871)
        self.google = GoogleCastVirtualReceiver()
        self.miracast = MiracastWfdVirtualReceiver(rtsp_port=7236)
        self.airplay = AirPlayVirtualReceiver(port=7000)
        self._api: ThreadingHTTPServer | None = None
        self._api_thread: threading.Thread | None = None

    def start(self) -> None:
        self.nearby.start()
        self.google.start()
        self.miracast.start()
        self.airplay.start()
        self._start_api()

    def stop(self) -> None:
        for device in (self.nearby, self.google, self.miracast, self.airplay):
            try:
                device.stop()
            except Exception as exc:
                print(f"[lab] stop error: {exc}", file=sys.stderr)
        if self._api:
            self._api.shutdown()
        if self._api_thread:
            self._api_thread.join(timeout=2)

    def _start_api(self) -> None:
        lab = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _json(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, indent=2).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def do_GET(self):
                if self.path in {"/", "/status"}:
                    snapshot = BUS.snapshot()
                    snapshot["lab_ip"] = lab.ip
                    snapshot["dashboard"] = "http://127.0.0.1:8765/"
                    self._json(200, snapshot)
                    return
                if self.path.startswith("/dashboard") or self.path == "/ui":
                    html = (ROOT / "dashboard" / "index.html").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html)
                    return
                self._json(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode() or "{}")
                except json.JSONDecodeError:
                    self._json(400, {"error": "invalid json"})
                    return
                if self.path == "/inject":
                    BUS.set_inject(**payload)
                    self._json(200, {"ok": True, "inject": BUS.inject})
                    return
                if self.path == "/action":
                    action = payload.get("action")
                    device = payload.get("device")
                    self._handle_action(device, action)
                    self._json(200, {"ok": True, "status": BUS.snapshot()})
                    return
                self._json(404, {"error": "not found"})

            def _handle_action(self, device: str, action: str) -> None:
                mapping = {
                    "nearbycast": lab.nearby,
                    "google_cast": lab.google,
                    "miracast": lab.miracast,
                    "airplay": lab.airplay,
                }
                target = mapping.get(device or "")
                if not target:
                    return
                if action == "restart":
                    target.stop()
                    time.sleep(0.2)
                    target.start()
                elif action == "stop":
                    target.stop()
                elif action == "start":
                    target.start()
                elif action == "crash":
                    BUS.set_inject(crash=True)
                    if hasattr(target, "stop"):
                        # Simulate receiver crash by stopping abruptly.
                        target.stop()
                        BUS.set_inject(crash=False)

        # Prefer 8765; fall back if busy.
        for port in (8765, 8766, 8767):
            try:
                self._api = ThreadingHTTPServer(("127.0.0.1", port), Handler)
                self.api_port = port
                break
            except OSError:
                continue
        else:
            raise RuntimeError("Could not bind virtual lab API port")
        self._api_thread = threading.Thread(target=self._api.serve_forever, daemon=True)
        self._api_thread.start()

    def banner(self) -> str:
        return f"""
NearbyCast Virtual Lab

Google Cast
  name: {self.google.name}
  address: {self.google.ip}
  dial: {self.google.dial_port}
  cast: {self.google.cast_port}
  load: {self.google.load_port}
  status: READY

Miracast (WFD RTSP/RTP lab — physical P2P NOT VERIFIED)
  name: {self.miracast.name}
  RTSP: {self.miracast.ip}:{self.miracast.rtsp_port}
  RTP: {self.miracast.rtp_port}
  status: READY

AirPlay (lab PIN + RTSP mirroring — FairPlay Apple TV NOT VERIFIED)
  name: {self.airplay.name}
  address: {self.airplay.ip}:{self.airplay.port}
  rtsp: {self.airplay.rtsp_port}
  media: {self.airplay.media_port}
  pin: {self.airplay.pairing_pin}
  status: READY (lab mirroring)

NearbyCast
  name: {self.nearby.name}
  address: {self.nearby.ip}:{self.nearby.port}
  pairing: {self.nearby.pairing_code}
  status: READY

Dashboard: http://127.0.0.1:{getattr(self, 'api_port', 8765)}/ui
API:       http://127.0.0.1:{getattr(self, 'api_port', 8765)}/status
""".strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once-status", action="store_true", help="Start, print banner, exit after --seconds")
    parser.add_argument("--seconds", type=float, default=0)
    args = parser.parse_args()

    lab = Lab()
    lab.start()
    print(lab.banner(), flush=True)

    def handle_signal(*_args):
        lab.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    if args.once_status:
        time.sleep(max(args.seconds, 0.5))
        lab.stop()
        return 0

    if args.seconds > 0:
        time.sleep(args.seconds)
        lab.stop()
        return 0

    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
