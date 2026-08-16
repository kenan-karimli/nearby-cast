#!/usr/bin/env python3
"""Stress / lifecycle tests: repeated start-stop, resource leak checks."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

os.environ["NEARBY_CAST_VIRTUAL_LAB"] = "1"
os.environ["NEARBY_CAST_ALLOW_LOOPBACK"] = "1"
os.environ["NEARBY_CAST_LAB_MEDIA"] = "1"

from airplay.receiver import AirPlayVirtualReceiver
from common import wait_until
from miracast.receiver import MiracastWfdVirtualReceiver
from nearbycast.receiver import NearbyCastVirtualReceiver


CYCLES = int(os.environ.get("NEARBY_CAST_STRESS_CYCLES", "20"))


def count_orphans() -> dict[str, int]:
    try:
        out = subprocess.check_output(["ps", "-u", str(os.getuid()), "-o", "comm="], text=True)
    except (OSError, subprocess.CalledProcessError):
        return {}
    counts: dict[str, int] = {}
    for name in ("ffmpeg", "wf-recorder", "python3"):
        counts[name] = sum(1 for line in out.splitlines() if line.strip() == name)
    return counts


def stress_miracast(cycles: int) -> tuple[bool, str]:
    sink = MiracastWfdVirtualReceiver(rtsp_port=17510)
    sink.start()
    failures = 0
    try:
        for index in range(cycles):
            session = Path(tempfile.mkdtemp(prefix=f"nc-stress-wfd-{index}-"))
            try:
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "miracast/lab_sender.py"),
                        "--target",
                        sink.ip,
                        "--rtsp-port",
                        str(sink.rtsp_port),
                        "--session-dir",
                        str(session),
                        "--duration",
                        "1",
                        "--no-audio",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if proc.returncode != 0:
                    failures += 1
            finally:
                shutil.rmtree(session, ignore_errors=True)
        ok = failures == 0 and wait_until(lambda: sink.state.metrics.received_packets > 0, 2)
        return ok, f"cycles={cycles} failures={failures} packets={sink.state.metrics.received_packets}"
    finally:
        sink.stop()


def stress_airplay(cycles: int) -> tuple[bool, str]:
    airplay = AirPlayVirtualReceiver(port=17520, pairing_pin="0000")
    airplay.start()
    failures = 0
    try:
        for index in range(cycles):
            session = Path(tempfile.mkdtemp(prefix=f"nc-stress-ap-{index}-"))
            try:
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "airplay/lab_sender.py"),
                        "--target",
                        airplay.ip,
                        "--http-port",
                        str(airplay.port),
                        "--pin",
                        "0000",
                        "--session-dir",
                        str(session),
                        "--duration",
                        "1",
                        "--no-audio",
                        "--lab-media",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if proc.returncode != 0:
                    failures += 1
            finally:
                shutil.rmtree(session, ignore_errors=True)
        ok = failures == 0 and airplay.state.metrics.received_bytes > 0
        return ok, f"cycles={cycles} failures={failures} bytes={airplay.state.metrics.received_bytes}"
    finally:
        airplay.stop()


def stress_nearby_pair_loop(cycles: int) -> tuple[bool, str]:
    import socket

    receiver = NearbyCastVirtualReceiver(port=29950, pairing_code="444 555")
    receiver.start()
    failures = 0
    try:
        for _ in range(cycles):
            try:
                sock = socket.create_connection((receiver.ip, receiver.port), timeout=3)
                sock.sendall(
                    json.dumps(
                        {
                            "type": "hello",
                            "device_id": f"stress-{time.time_ns()}",
                            "public_key_hex": "11" * 32,
                        }
                    ).encode()
                    + b"\n"
                )
                required = json.loads(sock.makefile().readline())
                if required.get("type") != "pair_required":
                    # Trusted after first pair — accept authorized too.
                    if required.get("type") != "authorized":
                        failures += 1
                        sock.close()
                        continue
                else:
                    sock.sendall(
                        json.dumps(
                            {
                                "type": "pair_submit",
                                "pairing_id": required["pairing_id"],
                                "code": "444 555",
                            }
                        ).encode()
                        + b"\n"
                    )
                    authorized = json.loads(sock.makefile().readline())
                    if authorized.get("type") != "authorized":
                        failures += 1
                sock.close()
            except Exception:
                failures += 1
        return failures == 0, f"cycles={cycles} failures={failures}"
    finally:
        receiver.stop()


def main() -> int:
    print(f"NearbyCast stress tests (cycles={CYCLES})\n", flush=True)
    before = count_orphans()
    rows: list[tuple[str, str, str]] = []

    for name, fn in (
        ("Miracast start/stop", lambda: stress_miracast(CYCLES)),
        ("AirPlay start/stop", lambda: stress_airplay(CYCLES)),
        ("NearbyCast pair loop", lambda: stress_nearby_pair_loop(CYCLES)),
    ):
        ok, detail = fn()
        rows.append((name, "PASS" if ok else "FAIL", detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    time.sleep(1)
    after = count_orphans()
    # ffmpeg/wf-recorder should not grow unboundedly vs baseline.
    ffmpeg_delta = after.get("ffmpeg", 0) - before.get("ffmpeg", 0)
    leak_ok = ffmpeg_delta <= 1
    rows.append(
        (
            "Orphan ffmpeg check",
            "PASS" if leak_ok else "FAIL",
            f"before={before} after={after} delta_ffmpeg={ffmpeg_delta}",
        )
    )
    print(f"[{'PASS' if leak_ok else 'FAIL'}] Orphan ffmpeg check: before={before} after={after}", flush=True)

    print("\n" + "=" * 40)
    print("Stress Results")
    print("=" * 40)
    ok = True
    for name, status, detail in rows:
        print(f"{name:<28} {status}  ({detail})")
        if status == "FAIL":
            ok = False
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    (ROOT / "last-stress-result.json").write_text(
        json.dumps({"ok": ok, "rows": rows, "cycles": CYCLES}, indent=2),
        encoding="utf-8",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
