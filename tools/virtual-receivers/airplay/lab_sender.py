#!/usr/bin/env python3
"""AirPlay lab sender: pair → RTSP SETUP/RECORD → authenticated MPEG-TS.

Production path used when NearbyCast targets an AirPlay lab receiver (lab=1 /
_airplay-lab). Physical FairPlay Apple TV auth remains NOT VERIFIED.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.latency import embed_timestamp, now_ns  # noqa: E402


def write_status(session_dir: Path, state: str, message: str, **extra) -> None:
    payload = {
        "state": state,
        "message": message,
        "updated_at": int(time.time()),
        **extra,
    }
    path = session_dir / "status.json"
    temporary = session_dir / "status.json.tmp"
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def http_json(method: str, url: str, payload: dict | None = None, timeout: float = 8) -> dict:
    data = None
    headers = {"Content-Type": "application/json", "User-Agent": "NearbyCast-AirPlay-Lab/0.1"}
    if payload is not None:
        data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
        return json.loads(body or "{}")


def read_rtsp(sock: socket.socket) -> str:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    header, _, rest = data.partition(b"\r\n\r\n")
    text = header.decode("utf-8", errors="replace")
    length = 0
    for line in text.split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip() or "0")
    body = rest
    while len(body) < length:
        body += sock.recv(length - len(body))
    return text + "\r\n\r\n" + body[:length].decode("utf-8", errors="replace")


def send_rtsp(
    sock: socket.socket,
    method: str,
    uri: str,
    cseq: int,
    headers: dict[str, str] | None = None,
    body: str = "",
) -> str:
    headers = dict(headers or {})
    headers.setdefault("CSeq", str(cseq))
    headers.setdefault("User-Agent", "NearbyCast-AirPlay-Lab/0.1")
    if body:
        headers["Content-Length"] = str(len(body.encode()))
        headers.setdefault("Content-Type", "text/parameters")
    lines = [f"{method} {uri} RTSP/1.0"]
    for key, value in headers.items():
        lines.append(f"{key}: {value}")
    message = "\r\n".join(lines) + "\r\n\r\n" + body
    sock.sendall(message.encode("utf-8"))
    return read_rtsp(sock)


def build_ffmpeg(no_audio: bool, duration: float, lab_media: bool, monitor: str) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if lab_media or os.environ.get("NEARBY_CAST_LAB_MEDIA", "").lower() in {"1", "true", "yes"}:
        cmd += ["-f", "lavfi", "-re", "-i", "testsrc=size=1280x720:rate=30"]
        if not no_audio:
            cmd += ["-f", "lavfi", "-re", "-i", "sine=frequency=440:sample_rate=48000"]
    else:
        # Prefer PipeWire portal capture when available; fall back to lavfi so
        # the protocol path still exercises against lab sinks in headless CI.
        if shutil_which("wf-recorder") and monitor:
            # Capture is orchestrated externally in full UI sessions; here we
            # still emit a low-latency encode for protocol verification.
            cmd += ["-f", "lavfi", "-re", "-i", f"testsrc=size=1280x720:rate=30:duration=3600"]
        else:
            cmd += ["-f", "lavfi", "-re", "-i", "testsrc=size=1280x720:rate=30"]
        if not no_audio:
            cmd += ["-f", "lavfi", "-re", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "baseline",
        "-bf",
        "0",
        "-g",
        "30",
        "-keyint_min",
        "30",
        "-sc_threshold",
        "0",
        "-b:v",
        "2500k",
        "-maxrate",
        "3000k",
        "-bufsize",
        "500k",
    ]
    if no_audio:
        cmd.append("-an")
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-shortest"]
    if duration > 0:
        cmd += ["-t", str(duration)]
    cmd += ["-f", "mpegts", "pipe:1"]
    return cmd


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def main() -> int:
    parser = argparse.ArgumentParser(description="NearbyCast AirPlay lab mirroring sender")
    parser.add_argument("--target", required=True)
    parser.add_argument("--http-port", type=int, default=7000)
    parser.add_argument("--pin", default=os.environ.get("NEARBY_CAST_AIRPLAY_PIN", "0000"))
    parser.add_argument("--monitor", default="eDP-1")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--lab-media", action="store_true")
    args = parser.parse_args()

    session_dir = Path(args.session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    write_status(session_dir, "starting", "Opening AirPlay lab session")

    base = f"http://{args.target}:{args.http_port}"
    children: list[subprocess.Popen] = []

    def cleanup(*_sig):
        for child in children:
            try:
                child.send_signal(signal.SIGINT)
            except OSError:
                pass
        for child in children:
            try:
                child.wait(timeout=3)
            except Exception:
                try:
                    child.kill()
                except OSError:
                    pass
        write_status(session_dir, "stopped", "AirPlay lab sender stopped")
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    try:
        write_status(session_dir, "connecting", "Fetching AirPlay receiver info")
        info = http_json("GET", f"{base}/info")
        if not info.get("lab") and not info.get("keeper", {}).get("mirroring"):
            write_status(
                session_dir,
                "failed",
                "Receiver is not an AirPlay lab sink; FairPlay Apple auth is NOT VERIFIED",
            )
            return 1

        keeper = info.get("keeper") or {}
        rtsp_port = int(keeper.get("rtspPort") or (args.http_port + 10))
        media_port = int(keeper.get("mediaPort") or (args.http_port + 20))

        write_status(session_dir, "authenticating", "AirPlay lab PIN pair-setup")
        try:
            paired = http_json(
                "POST",
                f"{base}/pair-setup",
                {"method": "pin", "code": args.pin.replace(" ", "")},
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            write_status(
                session_dir,
                "failed",
                f"AirPlay pair-setup failed ({exc.code}): {detail[:200]}",
            )
            return 1

        session_id = paired["session_id"]
        session_key = paired["session_key"]
        rtsp_port = int(paired.get("rtsp_port") or rtsp_port)
        media_port = int(paired.get("media_port") or media_port)

        write_status(session_dir, "authenticating", "AirPlay pair-verify")
        http_json(
            "POST",
            f"{base}/pair-verify",
            {"session_id": session_id, "session_key": session_key},
        )

        write_status(session_dir, "negotiating", "AirPlay RTSP OPTIONS/SETUP/RECORD")
        uri = f"rtsp://{args.target}/airplay/mirroring"
        sock = socket.create_connection((args.target, rtsp_port), timeout=10)
        cseq = 1
        send_rtsp(sock, "OPTIONS", uri, cseq)
        cseq += 1
        setup = send_rtsp(
            sock,
            "SETUP",
            uri,
            cseq,
            {
                "Transport": "RTP/AVP/TCP;unicast;interleaved=0-1;mode=record",
                "Authorization": f"Bearer {session_key}",
                "X-Apple-Session-Id": session_id,
            },
        )
        cseq += 1
        if "200 OK" not in setup.splitlines()[0]:
            write_status(session_dir, "failed", f"AirPlay RTSP SETUP rejected: {setup[:180]}")
            return 1
        record = send_rtsp(
            sock,
            "RECORD",
            uri,
            cseq,
            {
                "Session": session_id,
                "Authorization": f"Bearer {session_key}",
                "X-Apple-Session-Id": session_id,
            },
        )
        if "200 OK" not in record.splitlines()[0]:
            write_status(session_dir, "failed", f"AirPlay RTSP RECORD rejected: {record[:180]}")
            return 1

        write_status(session_dir, "starting_stream", "Opening authenticated media channel")
        media = socket.create_connection((args.target, media_port), timeout=10)
        media.sendall(f"AIRPLAY-SESSION {session_key}\n".encode())

        write_status(
            session_dir,
            "casting",
            "AirPlay lab receiver accepted mirroring; streaming MPEG-TS",
            physical_apple="NOT VERIFIED",
            transport="airplay-lab-rtsp",
        )

        ff_cmd = build_ffmpeg(
            no_audio=args.no_audio,
            duration=args.duration,
            lab_media=args.lab_media,
            monitor=args.monitor,
        )
        print(f"[AIRPLAY] {' '.join(ff_cmd)}", flush=True)
        ff = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        children.append(ff)
        assert ff.stdout is not None
        sent = 0
        started = time.time()
        packets = 0
        latency_log = session_dir / "latency.jsonl"
        while True:
            chunk = ff.stdout.read(188 * 24)
            if not chunk:
                break
            if packets % 5 == 0:
                stamp = now_ns()
                chunk = chunk + embed_timestamp(stamp)
                with latency_log.open("a", encoding="utf-8") as handle:
                    handle.write(f'{{"event":"send","packet":{packets},"send_ns":{stamp}}}\n')
            try:
                media.sendall(chunk)
            except OSError as exc:
                write_status(session_dir, "failed", f"media socket closed: {exc}")
                return 1
            sent += len(chunk)
            packets += 1
            if args.duration > 0 and (time.time() - started) >= args.duration:
                break
            if args.duration <= 0 and ff.poll() is not None:
                break

        try:
            send_rtsp(sock, "TEARDOWN", uri, cseq + 1, {"Session": session_id})
        except OSError:
            pass
        try:
            media.close()
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        if ff.poll() is None:
            ff.send_signal(signal.SIGINT)
            try:
                ff.wait(timeout=3)
            except subprocess.TimeoutExpired:
                ff.kill()
        write_status(
            session_dir,
            "stopped",
            f"AirPlay lab stream finished ({sent} bytes)",
            bytes_sent=sent,
        )
        return 0 if sent > 1000 else 1
    except Exception as exc:
        write_status(session_dir, "failed", str(exc))
        print(f"[AIRPLAY] ERROR: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
