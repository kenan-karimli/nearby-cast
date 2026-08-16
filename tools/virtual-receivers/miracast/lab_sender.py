#!/usr/bin/env python3
"""Miracast lab sender: WFD RTSP client + RTP MPEG-TS without Wi-Fi Direct.

Used by NearbyCast when the Miracast target is an IPv4 lab peer (not a P2P MAC).
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.latency import embed_timestamp, now_ns  # noqa: E402


def write_status(session_dir: Path, state: str, message: str) -> None:
    payload = {
        "state": state,
        "message": message,
        "updated_at": int(time.time()),
    }
    path = session_dir / "status.json"
    temporary = session_dir / "status.json.tmp"
    temporary.write_text(__import__("json").dumps(payload), encoding="utf-8")
    temporary.replace(path)


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


def send_rtsp(sock: socket.socket, method: str, uri: str, cseq: int, headers: dict[str, str] | None = None, body: str = "") -> str:
    headers = dict(headers or {})
    headers.setdefault("CSeq", str(cseq))
    headers.setdefault("User-Agent", "NearbyCast-WFD-Lab/0.1")
    if body:
        headers["Content-Length"] = str(len(body.encode()))
        headers.setdefault("Content-Type", "text/parameters")
    lines = [f"{method} {uri} RTSP/1.0"]
    for key, value in headers.items():
        lines.append(f"{key}: {value}")
    message = "\r\n".join(lines) + "\r\n\r\n" + body
    sock.sendall(message.encode("utf-8"))
    return read_rtsp(sock)


def parse_rtp_port(get_parameter_body: str) -> int:
    for line in get_parameter_body.splitlines():
        if "wfd_client_rtp_ports" in line:
            # RTP/AVP/UDP;unicast 19000 0 mode=play
            parts = line.split()
            for part in parts:
                if part.isdigit():
                    return int(part)
    raise RuntimeError("sink did not advertise wfd_client_rtp_ports")


def main() -> int:
    parser = argparse.ArgumentParser(description="NearbyCast Miracast lab RTSP/RTP sender")
    parser.add_argument("--target", required=True, help="Virtual WFD sink IPv4")
    parser.add_argument("--rtsp-port", type=int, default=7236)
    parser.add_argument("--monitor", default="eDP-1")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--duration", type=float, default=0, help="Optional auto-stop seconds")
    args = parser.parse_args()

    session_dir = Path(args.session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    write_status(session_dir, "starting", "Opening lab WFD RTSP session")

    uri = "rtsp://localhost/wfd1.0"
    sock = socket.create_connection((args.target, args.rtsp_port), timeout=10)
    cseq = 1
    try:
        write_status(session_dir, "negotiating", "WFD OPTIONS")
        send_rtsp(sock, "OPTIONS", uri, cseq, {"Require": "org.wfa.wfd1.0"})
        cseq += 1

        write_status(session_dir, "negotiating", "WFD GET_PARAMETER")
        response = send_rtsp(
            sock,
            "GET_PARAMETER",
            uri,
            cseq,
            body=(
                "wfd_video_formats\r\n"
                "wfd_audio_codecs\r\n"
                "wfd_client_rtp_ports\r\n"
                "wfd_content_protection\r\n"
            ),
        )
        cseq += 1
        rtp_port = parse_rtp_port(response)

        write_status(session_dir, "negotiating", "WFD SET_PARAMETER")
        send_rtsp(
            sock,
            "SET_PARAMETER",
            uri,
            cseq,
            body=(
                "wfd_video_formats: 00 00 01 01 000001FF 02 01 01 00 0001ffff 00 00 00 00\r\n"
                "wfd_audio_codecs: AAC 00000001 00\r\n"
                f"wfd_presentation_URL: rtsp://{args.target}:{args.rtsp_port}/wfd1.0/streamid=0 none\r\n"
                f"wfd_client_rtp_ports: RTP/AVP/UDP;unicast {rtp_port} 0 mode=play\r\n"
            ),
        )
        cseq += 1

        send_rtsp(sock, "SET_PARAMETER", uri, cseq, body="wfd_trigger_method: SETUP\r\n")
        cseq += 1

        setup = send_rtsp(
            sock,
            "SETUP",
            f"{uri}/streamid=0",
            cseq,
            {
                "Transport": f"RTP/AVP/UDP;unicast;client_port={rtp_port}-{rtp_port}",
            },
        )
        cseq += 1
        session = "1"
        for line in setup.splitlines():
            if line.lower().startswith("session:"):
                session = line.split(":", 1)[1].strip().split(";")[0].strip()

        play = send_rtsp(
            sock,
            "PLAY",
            f"{uri}/streamid=0",
            cseq,
            {"Session": session},
        )
        cseq += 1
        if "200 OK" not in play.splitlines()[0]:
            write_status(session_dir, "failed", "WFD PLAY was rejected")
            return 1

        write_status(session_dir, "casting", "Miracast lab sink accepted PLAY; sending RTP")
        print("PLAY accepted; media stream started", flush=True)

        duration = float(args.duration)
        ffmpeg = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-re",
            "-i",
            "testsrc=size=1280x720:rate=30",
        ]
        if not args.no_audio:
            ffmpeg += [
                "-f",
                "lavfi",
                "-re",
                "-i",
                "sine=frequency=440:sample_rate=48000",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-shortest",
            ]
        else:
            ffmpeg.append("-an")
        ffmpeg += [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "30",
            "-bf",
            "0",
        ]
        if duration > 0:
            ffmpeg += ["-t", str(duration)]
        ffmpeg += ["-f", "mpegts", "pipe:1"]

        proc = subprocess.Popen(ffmpeg, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        seq = 0
        timestamp = 0
        ssrc = 0x12345678
        latency_log = session_dir / "latency.jsonl"
        assert proc.stdout is not None
        try:
            while True:
                payload = proc.stdout.read(7 * 188)
                if not payload:
                    break
                if seq % 10 == 0:
                    stamp = now_ns()
                    payload = payload + embed_timestamp(stamp)
                    with latency_log.open("a", encoding="utf-8") as handle:
                        handle.write(f'{{"event":"send","seq":{seq},"send_ns":{stamp}}}\n')
                header = bytearray(12)
                header[0] = 0x80
                header[1] = 33  # MP2T
                header[2] = (seq >> 8) & 0xFF
                header[3] = seq & 0xFF
                header[4] = (timestamp >> 24) & 0xFF
                header[5] = (timestamp >> 16) & 0xFF
                header[6] = (timestamp >> 8) & 0xFF
                header[7] = timestamp & 0xFF
                header[8] = (ssrc >> 24) & 0xFF
                header[9] = (ssrc >> 16) & 0xFF
                header[10] = (ssrc >> 8) & 0xFF
                header[11] = ssrc & 0xFF
                rtp.sendto(bytes(header) + payload, (args.target, rtp_port))
                seq = (seq + 1) & 0xFFFF
                timestamp = (timestamp + 3000) & 0xFFFFFFFF
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            rtp.close()

        send_rtsp(sock, "TEARDOWN", f"{uri}/streamid=0", cseq, {"Session": session})
        write_status(session_dir, "idle", "Miracast lab session torn down")
        return 0
    except Exception as exc:
        write_status(session_dir, "failed", f"Miracast lab sender failed: {exc}")
        print(f"[FluxCast WFD] ERROR: {exc}", flush=True)
        return 1
    finally:
        try:
            sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
