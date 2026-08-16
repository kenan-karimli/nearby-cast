#!/usr/bin/env python3
"""Measure same-host lab latency using production Miracast/AirPlay senders."""
from __future__ import annotations

import json
import os
import shutil
import statistics
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


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def measure_miracast(duration: float = 4.0) -> dict:
    sink = MiracastWfdVirtualReceiver(rtsp_port=17410)
    sink.start()
    session = Path(tempfile.mkdtemp(prefix="nc-lat-wfd-"))
    started = time.time()
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
                str(duration),
                "--no-audio",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        wait_until(lambda: len(sink.state.metrics.latency_samples_ms) >= 5, 6)
        samples = list(sink.state.metrics.latency_samples_ms)
        startup = sink.state.metrics.first_frame_time_ms
        return {
            "protocol": "Miracast",
            "ok": proc.returncode == 0 and len(samples) >= 5,
            "startup_ms": startup,
            "sample_count": len(samples),
            "average_ms": statistics.fmean(samples) if samples else None,
            "p50_ms": sink.state.metrics.latency_p50_ms,
            "p95_ms": sink.state.metrics.latency_p95_ms,
            "p99_ms": sink.state.metrics.latency_p99_ms,
            "min_ms": min(samples) if samples else None,
            "max_ms": max(samples) if samples else None,
            "packets": sink.state.metrics.received_packets,
            "dropped": sink.state.metrics.dropped_packets,
            "bitrate_kbps": sink.state.metrics.bitrate_kbps,
            "fps": sink.state.metrics.average_fps,
            "duration_s": time.time() - started,
            "note": "same-host wall-clock; physical P2P NOT VERIFIED",
            "stderr_tail": (proc.stderr or "")[-200:],
        }
    finally:
        sink.stop()
        shutil.rmtree(session, ignore_errors=True)


def measure_airplay(duration: float = 4.0) -> dict:
    airplay = AirPlayVirtualReceiver(port=17420, pairing_pin="0000")
    airplay.start()
    session = Path(tempfile.mkdtemp(prefix="nc-lat-ap-"))
    started = time.time()
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
                str(duration),
                "--no-audio",
                "--lab-media",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        wait_until(lambda: len(airplay.state.metrics.latency_samples_ms) >= 5, 6)
        samples = list(airplay.state.metrics.latency_samples_ms)
        return {
            "protocol": "AirPlay",
            "ok": proc.returncode == 0 and len(samples) >= 5,
            "startup_ms": airplay.state.metrics.first_frame_time_ms,
            "sample_count": len(samples),
            "average_ms": statistics.fmean(samples) if samples else None,
            "p50_ms": airplay.state.metrics.latency_p50_ms,
            "p95_ms": airplay.state.metrics.latency_p95_ms,
            "p99_ms": airplay.state.metrics.latency_p99_ms,
            "min_ms": min(samples) if samples else None,
            "max_ms": max(samples) if samples else None,
            "packets": airplay.state.metrics.received_packets,
            "dropped": airplay.state.metrics.dropped_packets,
            "bitrate_kbps": airplay.state.metrics.bitrate_kbps,
            "fps": airplay.state.metrics.average_fps,
            "duration_s": time.time() - started,
            "note": "same-host wall-clock; FairPlay Apple TV NOT VERIFIED",
            "stderr_tail": (proc.stderr or "")[-200:],
        }
    finally:
        airplay.stop()
        shutil.rmtree(session, ignore_errors=True)


def main() -> int:
    print("NearbyCast latency lab (same-host, production senders)\n", flush=True)
    reports = [measure_miracast(), measure_airplay()]
    ok = True
    print("=" * 48)
    print("Latency Results")
    print("=" * 48)
    for report in reports:
        status = "PASS" if report["ok"] else "FAIL"
        if not report["ok"]:
            ok = False
        print(f"\n{report['protocol']}  {status}")
        print(f"  samples     {report['sample_count']}")
        print(f"  startup_ms  {fmt(report['startup_ms'])}")
        print(f"  average_ms  {fmt(report['average_ms'])}")
        print(f"  p50_ms      {fmt(report['p50_ms'])}")
        print(f"  p95_ms      {fmt(report['p95_ms'])}")
        print(f"  p99_ms      {fmt(report['p99_ms'])}")
        print(f"  packets     {report['packets']}")
        print(f"  note        {report['note']}")
        if not report["ok"] and report.get("stderr_tail"):
            print(f"  stderr      {report['stderr_tail']}")
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    out = ROOT / "last-latency-result.json"
    out.write_text(json.dumps({"ok": ok, "reports": reports}, indent=2), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
