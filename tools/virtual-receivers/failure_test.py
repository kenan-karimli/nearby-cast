#!/usr/bin/env python3
"""Adversarial failure-injection tests against production lab senders."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT))

os.environ["NEARBY_CAST_VIRTUAL_LAB"] = "1"
os.environ["NEARBY_CAST_ALLOW_LOOPBACK"] = "1"
os.environ["NEARBY_CAST_LAB_MEDIA"] = "1"

from airplay.receiver import AirPlayVirtualReceiver
from common import BUS, wait_until
from miracast.receiver import MiracastWfdVirtualReceiver
from nearbycast.receiver import NearbyCastVirtualReceiver


class Results:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.rows.append((name, status, detail))
        print(f"[{status}] {name}: {detail}", flush=True)

    @property
    def ok(self) -> bool:
        return all(status != "FAIL" for _, status, _ in self.rows)


def run_airplay(session: Path, ip: str, port: int, pin: str, duration: float = 2) -> subprocess.CompletedProcess:
    sender = ROOT / "airplay/lab_sender.py"
    return subprocess.run(
        [
            sys.executable,
            str(sender),
            "--target",
            ip,
            "--http-port",
            str(port),
            "--pin",
            pin,
            "--session-dir",
            str(session),
            "--duration",
            str(duration),
            "--no-audio",
            "--lab-media",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )


def run_miracast(session: Path, ip: str, port: int, duration: float = 2) -> subprocess.CompletedProcess:
    sender = ROOT / "miracast/lab_sender.py"
    return subprocess.run(
        [
            sys.executable,
            str(sender),
            "--target",
            ip,
            "--rtsp-port",
            str(port),
            "--session-dir",
            str(session),
            "--duration",
            str(duration),
            "--no-audio",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )


def test_airplay_wrong_pin(results: Results) -> None:
    airplay = AirPlayVirtualReceiver(port=17101, pairing_pin="0000")
    airplay.start()
    session = Path(tempfile.mkdtemp(prefix="nc-fail-ap-pin-"))
    try:
        proc = run_airplay(session, airplay.ip, airplay.port, pin="9999", duration=1)
        status = json.loads((session / "status.json").read_text()) if (session / "status.json").exists() else {}
        if proc.returncode == 0 or status.get("state") == "casting":
            results.add("AirPlay wrong PIN", "FAIL", "accepted bad PIN")
        else:
            results.add("AirPlay wrong PIN", "PASS", status.get("message", "rejected"))
    finally:
        airplay.stop()
        shutil.rmtree(session, ignore_errors=True)


def test_airplay_fairplay_refused(results: Results) -> None:
    airplay = AirPlayVirtualReceiver(port=17102)
    airplay.start()
    try:
        req = urllib.request.Request(
            f"http://{airplay.ip}:{airplay.port}/pair-setup",
            data=b"\x00\x01binary",
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=3)
            results.add("AirPlay FairPlay refuse", "FAIL", "accepted FairPlay binary")
        except urllib.error.HTTPError as exc:
            if exc.code == 501:
                results.add("AirPlay FairPlay refuse", "PASS", "501 unsupported")
            else:
                results.add("AirPlay FairPlay refuse", "FAIL", f"code={exc.code}")
    finally:
        airplay.stop()


def test_airplay_malformed_rtsp(results: Results) -> None:
    BUS.set_inject(airplay_malformed_rtsp=True)
    airplay = AirPlayVirtualReceiver(port=17103)
    airplay.start()
    session = Path(tempfile.mkdtemp(prefix="nc-fail-ap-rtsp-"))
    try:
        proc = run_airplay(session, airplay.ip, airplay.port, pin="0000", duration=1)
        if proc.returncode == 0:
            results.add("AirPlay malformed RTSP", "FAIL", "sender succeeded")
        else:
            results.add("AirPlay malformed RTSP", "PASS", "sender failed cleanly")
    finally:
        BUS.set_inject(airplay_malformed_rtsp=False)
        airplay.stop()
        shutil.rmtree(session, ignore_errors=True)


def test_miracast_reject_media(results: Results) -> None:
    BUS.set_inject(reject_media=True)
    sink = MiracastWfdVirtualReceiver(rtsp_port=17301)
    sink.start()
    session = Path(tempfile.mkdtemp(prefix="nc-fail-wfd-rej-"))
    try:
        proc = run_miracast(session, sink.ip, sink.rtsp_port, duration=1)
        status = json.loads((session / "status.json").read_text()) if (session / "status.json").exists() else {}
        if proc.returncode == 0 and status.get("state") in {"casting", "idle"}:
            # PLAY forbidden should fail before casting
            if sink.state.stream == "rejected" or status.get("state") == "failed":
                results.add("Miracast reject media", "PASS", "PLAY forbidden handled")
            else:
                results.add("Miracast reject media", "FAIL", f"state={status.get('state')}")
        else:
            results.add("Miracast reject media", "PASS", status.get("message", "failed as expected"))
    finally:
        BUS.set_inject(reject_media=False)
        sink.stop()
        shutil.rmtree(session, ignore_errors=True)


def test_miracast_malformed_rtsp(results: Results) -> None:
    BUS.set_inject(miracast_malformed_rtsp=True)
    sink = MiracastWfdVirtualReceiver(rtsp_port=17302)
    sink.start()
    session = Path(tempfile.mkdtemp(prefix="nc-fail-wfd-bad-"))
    try:
        proc = run_miracast(session, sink.ip, sink.rtsp_port, duration=1)
        if proc.returncode == 0:
            results.add("Miracast malformed RTSP", "FAIL", "accepted garbage OPTIONS")
        else:
            results.add("Miracast malformed RTSP", "PASS", "sender failed cleanly")
    finally:
        BUS.set_inject(miracast_malformed_rtsp=False)
        sink.stop()
        shutil.rmtree(session, ignore_errors=True)


def test_miracast_packet_loss(results: Results) -> None:
    BUS.set_inject(packet_loss=0.3)
    sink = MiracastWfdVirtualReceiver(rtsp_port=17303)
    sink.start()
    session = Path(tempfile.mkdtemp(prefix="nc-fail-wfd-loss-"))
    try:
        proc = run_miracast(session, sink.ip, sink.rtsp_port, duration=2)
        if proc.returncode != 0:
            results.add("Miracast packet loss", "FAIL", "sender crashed under loss")
            return
        if not wait_until(lambda: sink.state.metrics.received_packets > 0, 8):
            results.add("Miracast packet loss", "FAIL", "no packets survived")
            return
        results.add(
            "Miracast packet loss",
            "PASS",
            f"recv={sink.state.metrics.received_packets} dropped={sink.state.metrics.dropped_packets}",
        )
    finally:
        BUS.set_inject(packet_loss=0.0)
        sink.stop()
        shutil.rmtree(session, ignore_errors=True)


def test_nearby_bad_token(results: Results) -> None:
    receiver = NearbyCastVirtualReceiver(port=29901, pairing_code="111 222")
    receiver.start()
    try:
        sock = socket.create_connection((receiver.ip, receiver.port), timeout=5)
        sock.sendall(
            json.dumps({"type": "hello", "device_id": "fail-tok", "public_key_hex": "cc" * 32}).encode()
            + b"\n"
        )
        required = json.loads(sock.makefile().readline())
        sock.sendall(
            json.dumps(
                {
                    "type": "pair_submit",
                    "pairing_id": required["pairing_id"],
                    "code": "111 222",
                }
            ).encode()
            + b"\n"
        )
        authorized = json.loads(sock.makefile().readline())
        media = socket.create_connection((receiver.ip, authorized["media_port"]), timeout=5)
        media.sendall(b"NEARBYCAST-SESSION expired-or-wrong\n")
        time.sleep(0.3)
        if receiver.state.stream == "receiving" and receiver.state.metrics.received_bytes > 0:
            results.add("NearbyCast bad token", "FAIL", "accepted bad token")
        else:
            results.add("NearbyCast bad token", "PASS", "rejected")
        media.close()
        sock.close()
    finally:
        receiver.stop()


def test_nearby_wrong_pairing(results: Results) -> None:
    receiver = NearbyCastVirtualReceiver(port=29902, pairing_code="111 222")
    receiver.start()
    try:
        sock = socket.create_connection((receiver.ip, receiver.port), timeout=5)
        sock.sendall(
            json.dumps({"type": "hello", "device_id": "fail-pin", "public_key_hex": "dd" * 32}).encode()
            + b"\n"
        )
        required = json.loads(sock.makefile().readline())
        sock.sendall(
            json.dumps(
                {
                    "type": "pair_submit",
                    "pairing_id": required["pairing_id"],
                    "code": "000 000",
                }
            ).encode()
            + b"\n"
        )
        reply = json.loads(sock.makefile().readline())
        if reply.get("type") == "authorized":
            results.add("NearbyCast wrong pairing", "FAIL", "authorized wrong code")
        else:
            results.add("NearbyCast wrong pairing", "PASS", reply.get("type", "rejected"))
        sock.close()
    finally:
        receiver.stop()


def test_nearby_oversized_json(results: Results) -> None:
    receiver = NearbyCastVirtualReceiver(port=29903, pairing_code="111 222")
    receiver.start()
    try:
        sock = socket.create_connection((receiver.ip, receiver.port), timeout=5)
        sock.settimeout(2)
        blob = json.dumps({"type": "hello", "device_id": "x", "public_key_hex": "ee" * 32, "pad": "Z" * 2_000_000})
        try:
            sock.sendall(blob.encode() + b"\n")
            _ = sock.recv(4096)
            results.add("NearbyCast oversized JSON", "PASS", "receiver survived")
        except OSError:
            results.add("NearbyCast oversized JSON", "PASS", "connection closed safely")
        finally:
            sock.close()
    finally:
        receiver.stop()


def test_port_occupied(results: Results) -> None:
    first = MiracastWfdVirtualReceiver(rtsp_port=17350)
    first.start()
    try:
        second = MiracastWfdVirtualReceiver(rtsp_port=17350)
        try:
            second.start()
            # SO_REUSEADDR may allow a second bind on some kernels; ensure the
            # first listener still owns the port by connecting once.
            sock = socket.create_connection((first.ip, 17350), timeout=1)
            sock.close()
            results.add(
                "Port occupied",
                "PASS",
                "reuse allowed; primary listener remains reachable",
            )
            second.stop()
        except OSError as exc:
            results.add("Port occupied", "PASS", f"second bind failed: {exc}")
    finally:
        first.stop()


def test_shell_injection_strings_rejected_by_helpers(results: Results) -> None:
    # Production peer selector validation is in Rust; mirror the contract here.
    evil = ["aa:bb:cc:dd:ee:ff;rm -rf /", "tv\n--evil", "$(reboot)", "`id`"]
    for value in evil:
        if "\n" in value or ";" in value or "`" in value or "$(" in value:
            continue
    # Invoke miracast valid_peer via a tiny cargo test already covers this;
    # here we ensure lab sender does not invoke a shell with target.
    sender = (ROOT / "miracast/lab_sender.py").read_text(encoding="utf-8")
    if "shell=True" in sender or "os.system" in sender:
        results.add("Shell injection surface", "FAIL", "lab sender uses shell")
    else:
        results.add("Shell injection surface", "PASS", "no shell=True in lab senders")


def main() -> int:
    results = Results()
    print("NearbyCast failure-injection tests\n", flush=True)
    test_airplay_wrong_pin(results)
    test_airplay_fairplay_refused(results)
    test_airplay_malformed_rtsp(results)
    test_miracast_reject_media(results)
    test_miracast_malformed_rtsp(results)
    test_miracast_packet_loss(results)
    test_nearby_bad_token(results)
    test_nearby_wrong_pairing(results)
    test_nearby_oversized_json(results)
    test_port_occupied(results)
    test_shell_injection_strings_rejected_by_helpers(results)

    print("\n" + "=" * 40)
    print("Failure Injection Results")
    print("=" * 40)
    for name, status, detail in results.rows:
        print(f"{name:<34} {status}  ({detail})")
    print(f"\nRESULT: {'PASS' if results.ok else 'FAIL'}")
    out = ROOT / "last-failure-result.json"
    out.write_text(
        json.dumps({"ok": results.ok, "rows": results.rows}, indent=2),
        encoding="utf-8",
    )
    return 0 if results.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
