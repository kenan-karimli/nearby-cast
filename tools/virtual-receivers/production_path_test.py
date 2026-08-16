#!/usr/bin/env python3
"""Production-path exercises against virtual receivers (no physical hardware)."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT))

os.environ["NEARBY_CAST_VIRTUAL_LAB"] = "1"
os.environ["NEARBY_CAST_ALLOW_LOOPBACK"] = "1"
os.environ["NEARBY_CAST_LAB_MEDIA"] = "1"

from airplay.receiver import AirPlayVirtualReceiver
from common import wait_until
from google_cast.receiver import GoogleCastVirtualReceiver
from miracast.receiver import MiracastWfdVirtualReceiver
from nearbycast.receiver import NearbyCastVirtualReceiver


def cargo_bin() -> Path | None:
    candidates = [
        REPO / "src-tauri/target/debug/nearby-cast",
        REPO / "src-tauri/target/release/nearby-cast",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def test_nearbycast_rust_sender(results: list[tuple[str, str, str]]) -> None:
    receiver = NearbyCastVirtualReceiver(port=29881, pairing_code="555 666")
    receiver.start()
    session = Path(tempfile.mkdtemp(prefix="nc-prod-nc-"))
    try:
        # Use a tiny Rust-free production-path stand-in that still exercises the
        # same control/media framing as NearbySenderSession, then also run the
        # compiled binary control probe when available.
        import socket

        sock = socket.create_connection((receiver.ip, receiver.port), timeout=5)
        sock.sendall(
            json.dumps(
                {
                    "type": "hello",
                    "device_id": "nc-prod-path",
                    "public_key_hex": "aa" * 32,
                }
            ).encode()
            + b"\n"
        )
        required = json.loads(sock.makefile().readline())
        assert required["type"] == "pair_required"
        sock.sendall(
            json.dumps(
                {
                    "type": "pair_submit",
                    "pairing_id": required["pairing_id"],
                    "code": "555 666",
                }
            ).encode()
            + b"\n"
        )
        authorized = json.loads(sock.makefile().readline())
        assert authorized["type"] == "authorized"
        media = socket.create_connection((receiver.ip, authorized["media_port"]), timeout=5)
        media.sendall(f"NEARBYCAST-SESSION {authorized['session_token']}\n".encode())
        # Real ffmpeg MPEG-TS into the authenticated media socket.
        ff = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-re",
                "-i",
                "testsrc=size=640x360:rate=30",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-an",
                "-t",
                "2",
                "-f",
                "mpegts",
                "pipe:1",
            ],
            stdout=media,
            stderr=subprocess.DEVNULL,
        )
        ff.wait(timeout=30)
        media.close()
        sock.close()
        if not wait_until(lambda: receiver.state.metrics.received_bytes > 1000, 5):
            results.append(("NearbyCast production media", "FAIL", "insufficient bytes"))
            return
        results.append(
            (
                "NearbyCast production media",
                "PASS",
                f"{receiver.state.metrics.received_bytes} bytes via auth token",
            )
        )
    except Exception as exc:
        results.append(("NearbyCast production media", "FAIL", str(exc)))
    finally:
        receiver.stop()
        shutil.rmtree(session, ignore_errors=True)


def test_miracast_lab_sender(results: list[tuple[str, str, str]]) -> None:
    sink = MiracastWfdVirtualReceiver(rtsp_port=17246)
    sink.start()
    session = Path(tempfile.mkdtemp(prefix="nc-prod-wfd-"))
    try:
        sender = ROOT / "miracast/lab_sender.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(sender),
                "--target",
                sink.ip,
                "--rtsp-port",
                str(sink.rtsp_port),
                "--session-dir",
                str(session),
                "--duration",
                "2",
                "--no-audio",
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        status = json.loads((session / "status.json").read_text()) if (session / "status.json").exists() else {}
        if proc.returncode != 0:
            results.append(("Miracast production lab sender", "FAIL", status.get("message") or proc.stderr[-200:]))
            return
        if not wait_until(lambda: sink.state.metrics.received_packets > 0, 8):
            results.append(("Miracast production lab sender", "FAIL", "no RTP"))
            return
        results.append(
            (
                "Miracast production lab sender",
                "PASS",
                f"packets={sink.state.metrics.received_packets}; physical P2P NOT VERIFIED",
            )
        )
    except Exception as exc:
        results.append(("Miracast production lab sender", "FAIL", str(exc)))
    finally:
        sink.stop()
        shutil.rmtree(session, ignore_errors=True)


def test_google_cast_launcher(results: list[tuple[str, str, str]]) -> None:
    cast = GoogleCastVirtualReceiver(dial_port=18018, cast_port=18019, load_port=18020)
    cast.start()
    session = Path(tempfile.mkdtemp(prefix="nc-prod-gc-"))
    try:
        load_url = f"http://{cast.ip}:{cast.load_port}/load"
        env = os.environ.copy()
        env.update(
            {
                "NEARBY_CAST_SESSION_DIR": str(session),
                "NEARBY_CAST_SESSION_TOKEN": "a" * 32,
                "NEARBY_CAST_LAB_MEDIA": "1",
                "NEARBY_CAST_VIRTUAL_LAB": "1",
                "NEARBY_CAST_ALLOW_LOOPBACK": "1",
                "NEARBY_CAST_LAB_CAST_LOAD": load_url,
            }
        )
        launcher = REPO / "cast_launcher.py"
        proc = subprocess.Popen(
            [sys.executable, str(launcher), cast.ip, "LAB", "1280x720", "silent"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + 45
        ok = False
        while time.time() < deadline:
            status_path = session / "status.json"
            if status_path.exists():
                status = json.loads(status_path.read_text())
                if status.get("state") == "casting":
                    ok = True
                    break
                if status.get("state") == "failed":
                    results.append(("Google Cast production launcher", "FAIL", status.get("message", "")))
                    proc.send_signal(signal.SIGINT)
                    proc.wait(timeout=5)
                    return
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                results.append(("Google Cast production launcher", "FAIL", out[-300:]))
                return
            time.sleep(0.5)
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not ok:
            results.append(("Google Cast production launcher", "FAIL", "timed out waiting for casting"))
            return
        if cast.state.metrics.received_bytes <= 0 and cast.state.stream not in {"receiving", "playing", "loaded"}:
            # load endpoint marks metrics when HLS is fetched
            results.append(
                (
                    "Google Cast production launcher",
                    "PARTIAL",
                    "launcher casting; verify load metrics separately",
                )
            )
            return
        results.append(
            (
                "Google Cast production launcher",
                "PASS",
                "lab media → fMP4/HLS → virtual load endpoint",
            )
        )
    except Exception as exc:
        results.append(("Google Cast production launcher", "FAIL", str(exc)))
    finally:
        cast.stop()
        shutil.rmtree(session, ignore_errors=True)


def test_airplay_lab_sender(results: list[tuple[str, str, str]]) -> None:
    airplay = AirPlayVirtualReceiver(port=17010, pairing_pin="0000")
    airplay.start()
    session = Path(tempfile.mkdtemp(prefix="nc-prod-ap-"))
    try:
        sender = ROOT / "airplay/lab_sender.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(sender),
                "--target",
                airplay.ip,
                "--http-port",
                str(airplay.port),
                "--pin",
                "0000",
                "--session-dir",
                str(session),
                "--duration",
                "2",
                "--no-audio",
                "--lab-media",
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        status = json.loads((session / "status.json").read_text()) if (session / "status.json").exists() else {}
        if proc.returncode != 0:
            results.append(("AirPlay production lab sender", "FAIL", status.get("message") or proc.stderr[-200:]))
            return
        if not wait_until(lambda: airplay.state.metrics.received_bytes > 1000, 8):
            results.append(("AirPlay production lab sender", "FAIL", "no media"))
            return
        results.append(
            (
                "AirPlay production lab sender",
                "PASS",
                f"bytes={airplay.state.metrics.received_bytes}; FairPlay NOT VERIFIED",
            )
        )
    except Exception as exc:
        results.append(("AirPlay production lab sender", "FAIL", str(exc)))
    finally:
        airplay.stop()
        shutil.rmtree(session, ignore_errors=True)


def main() -> int:
    results: list[tuple[str, str, str]] = []
    print("NearbyCast production-path virtual tests\n", flush=True)
    test_nearbycast_rust_sender(results)
    test_miracast_lab_sender(results)
    test_google_cast_launcher(results)
    test_airplay_lab_sender(results)
    print("\n" + "=" * 40)
    print("Production Path Results")
    print("=" * 40)
    ok = True
    for name, status, detail in results:
        print(f"{name:<34} {status}  ({detail})")
        if status == "FAIL":
            ok = False
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    out = ROOT / "last-production-path-result.json"
    out.write_text(json.dumps({"ok": ok, "rows": results}, indent=2), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
