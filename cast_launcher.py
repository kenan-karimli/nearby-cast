#!/usr/bin/env python3
"""
Nearby Cast — Direct Capture Streaming Launcher

Pipeline:
  wf-recorder --muxer=rawvideo -c rawvideo -x bgr0 -f pipe
  | ffmpeg -> low-latency H.264
  | HLS (default, ~3–5s) or fragmented MP4 growing-file (compatible, often slower)

Default transport is fragmented MP4 (`NEARBY_CAST_TRANSPORT=fmp4`) — verified
PLAYING on Android TV. Optional HLS (`hls`) is lower latency when the box
accepts it; ultra-short HLS previously grey-screened.
"""

import sys
import os
import re
import json
import time
import subprocess
import socket
import http.server
import socketserver
import threading
import signal
import shutil
import urllib.request
from urllib.parse import urlsplit
from glob import glob

# The Tauri process creates this directory with mode 0700 for every session.
# Keeping all child resources below it prevents one session from touching a
# concurrent or previously failed one.
SESSION_DIR = os.environ.get("NEARBY_CAST_SESSION_DIR", "")
SESSION_TOKEN = os.environ.get("NEARBY_CAST_SESSION_TOKEN", "")
if not SESSION_DIR or not SESSION_TOKEN:
    raise RuntimeError("NearbyCast session directory and authorization token are required")
MEDIA_DIR = os.path.join(SESSION_DIR, "media")
HLS_DIR = MEDIA_DIR  # retained alias for older helpers/tests
STATUS_FILE = os.path.join(SESSION_DIR, "status.json")
PROGRESS_FILE = os.path.join(SESSION_DIR, "ffmpeg-progress.log")
PIDS_FILE = os.path.join(SESSION_DIR, "children.json")
WFR_LOG = os.path.join(SESSION_DIR, "wf-recorder.log")
FFM_LOG = os.path.join(SESSION_DIR, "ffmpeg.log")
FMP4_PATH = os.path.join(MEDIA_DIR, "live.mp4")
HLS_PATH = os.path.join(MEDIA_DIR, "live.m3u8")
REMOTE_MEDIA_CLIENTS = set()
REMOTE_MEDIA_LOCK = threading.Lock()
SELECTED_ENCODER = "uninitialized"
# fMP4 is the verified path for Android TV / Cast boxes on this project.
# HLS (~3–5s) is faster when it works: NEARBY_CAST_TRANSPORT=hls
# Growing-file fMP4 often buffers ~10–20s on Default Media Receiver.
TRANSPORT = os.environ.get("NEARBY_CAST_TRANSPORT", "fmp4").strip().lower()
if TRANSPORT not in {"fmp4", "hls"}:
    raise RuntimeError(f"Unsupported NEARBY_CAST_TRANSPORT={TRANSPORT!r}")

HTML_PLAYER = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Nearby Cast — Live Screen</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { background:#09090b; overflow:hidden; width:100vw; height:100vh; display:flex; align-items:center; justify-content:center; }
        video { width:100%; height:100%; object-fit:contain; }
        .badge { position:absolute; top:16px; left:16px; background:rgba(24,24,27,0.85);
                 backdrop-filter:blur(12px); border:1px solid #3f3f46; padding:8px 14px;
                 border-radius:20px; font-size:13px; font-weight:600; color:#f4f4f5;
                 display:flex; align-items:center; gap:8px; font-family:system-ui; z-index:10; }
        .dot { width:8px; height:8px; border-radius:50%; background:#22c55e; animation:blink 1.2s infinite; }
        @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.3} }
    </style>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
</head>
<body>
    <div class="badge"><span class="dot"></span> Nearby Cast · Live</div>
    <video id="video" autoplay playsinline controls muted></video>
    <script>
        const video = document.getElementById('video');
        const params = new URLSearchParams(location.search);
        const prefer = params.get('src') || 'live.mp4';
        if (prefer.endsWith('.mp4')) {
            video.src = '/' + prefer;
            video.play().catch(()=>{});
        } else {
            const src = '/' + prefer;
            if (window.Hls && Hls.isSupported()) {
                const hls = new Hls({ maxBufferLength:2, liveSyncDurationCount:1,
                                       liveMaxLatencyDurationCount:2, enableWorker:true,
                                       lowLatencyMode:true });
                hls.loadSource(src);
                hls.attachMedia(video);
                hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(()=>{}));
            } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
                video.src = src;
                video.addEventListener('loadedmetadata', () => video.play().catch(()=>{}));
            }
        }
    </script>
</body>
</html>
"""

def get_local_ip(target: str) -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None

def write_json(path: str, payload: dict):
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(temporary_path, path)

def write_status(state: str, message: str = "", **extra):
    payload = {"state": state, "message": message, "updated_at": time.time(), **extra}
    write_json(STATUS_FILE, payload)
    print(f"[STATUS] {state}: {message}", flush=True)

def stop_recorded_processes():
    try:
        with open(PIDS_FILE, encoding="utf-8") as handle:
            pids = json.load(handle)
    except (OSError, json.JSONDecodeError):
        pids = []

    for pid in pids:
        try:
            with open(f"/proc/{int(pid)}/cmdline", "rb") as handle:
                command_line = handle.read().decode("utf-8", errors="replace")
            if "wf-recorder" not in command_line and "ffmpeg" not in command_line:
                continue
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            pass

    try:
        os.unlink(PIDS_FILE)
    except OSError:
        pass

def stop_processes(wfr_proc, ffm_proc):
    """Stop both pipeline children and reap them before removing session files."""
    seen = set()
    for process in (wfr_proc, ffm_proc):
        if process is None or process.pid in seen:
            continue
        seen.add(process.pid)
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
    deadline = time.monotonic() + 3
    for process in (wfr_proc, ffm_proc):
        if process is None or process.pid in seen and process.poll() is not None:
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except (subprocess.TimeoutExpired, OSError):
                pass
    for process in (wfr_proc, ffm_proc):
        if process is None or process.poll() is not None:
            continue
        try:
            process.kill()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass

def cleanup(remove_status: bool = True):
    stop_recorded_processes()
    paths = [PROGRESS_FILE]
    if remove_status:
        paths.append(STATUS_FILE)
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass
    pipe = os.path.join(SESSION_DIR, "capture.raw.pipe")
    try:
        os.unlink(pipe)
    except OSError:
        pass

def make_env():
    env = os.environ.copy()
    if not env.get("XDG_RUNTIME_DIR"):
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    if not env.get("WAYLAND_DISPLAY"):
        env["WAYLAND_DISPLAY"] = "wayland-1"
    return env

def get_monitor_resolution(output_name: str = "") -> tuple | None:
    """Return the actual capture resolution, preferring the requested output."""
    try:
        result = subprocess.run(
            ["wlr-randr"], capture_output=True, text=True, env=make_env(), timeout=3
        )
        current_output = ""
        for line in result.stdout.splitlines():
            if line and not line[0].isspace():
                current_output = line.split(None, 1)[0]
            elif (not output_name or current_output == output_name) and "current" in line:
                match = re.search(r"(\d+)x(\d+)", line)
                if match:
                    return int(match.group(1)), int(match.group(2))
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["hyprctl", "monitors", "-j"],
            capture_output=True, text=True, env=make_env(), timeout=3
        )
        monitors = json.loads(result.stdout)
        if monitors:
            m = next((monitor for monitor in monitors if monitor.get("name") == output_name), monitors[0])
            width, height = m.get("width"), m.get("height")
            if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
                return width, height
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["xrandr", "--query"], capture_output=True, text=True, timeout=3
        )
        for line in result.stdout.splitlines():
            if " connected" not in line:
                continue
            if output_name and not line.startswith(f"{output_name} "):
                continue
            match = re.search(r"(\d+)x(\d+)\+\d+\+\d+", line)
            if match:
                return int(match.group(1)), int(match.group(2))
    except Exception:
        pass
    return None

def parse_window_geometry(geometry: str) -> str:
    """
    Parse 'X,Y WxH' (hyprctl format) into wf-recorder -g string.
    Returns None if full screen.
    """
    if not geometry or geometry.upper() == "PORTAL":
        return None
    geom = geometry.strip()
    m = re.search(r'(\d+),(\d+)\s+(\d+)x(\d+)', geom)
    if m:
        x, y, w, h = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        w = max(2, w - (w % 2))
        h = max(2, h - (h % 2))
        return f"{x},{y} {w}x{h}"
    m2 = re.search(r'(\d+)x(\d+)\+(\d+)\+(\d+)', geom)
    if m2:
        w, h, x, y = int(m2.group(1)), int(m2.group(2)), int(m2.group(3)), int(m2.group(4))
        w = max(2, w - (w % 2))
        h = max(2, h - (h % 2))
        return f"{x},{y} {w}x{h}"
    return None

def probe_encoder(command: list[str]) -> bool:
    """Return true only when FFmpeg can complete a real one-frame encode."""
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=8,
            env=make_env(),
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False

def select_video_encoder() -> tuple[str, list[str], list[str]]:
    """Probe suitable H.264 encoders and return name, global, and video args.

    Listing an encoder is not proof that a driver/device is usable. Every
    hardware candidate below performs a one-frame encode before selection.
    """
    forced = os.environ.get("NEARBY_CAST_ENCODER", "auto").lower()
    if forced not in {"auto", "h264_vaapi", "h264_nvenc", "h264_qsv", "libx264"}:
        raise RuntimeError(f"Unsupported requested encoder: {forced}")

    # GOP 15 @ 30fps ≈ 0.5s — shorter fragments / HLS segments for lower delay.
    gop = "15"
    candidates = []
    for device in glob("/dev/dri/renderD*"):
        candidates.append((
            "h264_vaapi",
            ["-init_hw_device", f"vaapi=va:{device}", "-filter_hw_device", "va"],
            ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-bf", "0", "-g", gop, "-b:v", "4M"],
        ))
        candidates.append((
            "h264_vaapi",
            ["-vaapi_device", device],
            ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-bf", "0", "-g", gop, "-b:v", "4M"],
        ))
    candidates.extend([
        ("h264_nvenc", [], ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull", "-bf", "0", "-g", gop, "-b:v", "4M"]),
        ("h264_qsv", [], ["-c:v", "h264_qsv", "-preset", "veryfast", "-bf", "0", "-g", gop, "-b:v", "4M"]),
        ("libx264", [], [
            "-c:v", "libx264", "-profile:v", "baseline", "-level", "4.0",
            "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
            "-bf", "0", "-g", gop, "-keyint_min", gop, "-sc_threshold", "0",
            "-b:v", "3500k", "-maxrate", "4000k", "-bufsize", "700k",
        ]),
    ])

    for name, global_args, video_args in candidates:
        if forced != "auto" and forced != name:
            continue
        probe = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *global_args,
            "-f", "lavfi", "-i", "color=c=black:s=64x64:r=1", "-frames:v", "1",
            *video_args, "-f", "null", "-",
        ]
        if probe_encoder(probe):
            print(f"[ENCODER] Selected {name} after successful probe", flush=True)
            return name, global_args, video_args
        print(f"[ENCODER] {name} probe failed", flush=True)

    if forced != "auto":
        raise RuntimeError(f"Requested encoder {forced} failed its runtime probe")
    raise RuntimeError("No usable H.264 encoder was found; install or repair FFmpeg/libx264")

class UniversalHandler(http.server.BaseHTTPRequestHandler):
    """Session-token gated media server.

    fMP4 is served as an HTTP/1.0 growing-file stream without Content-Length.
    That is the path verified to reach PLAYING on Android TV Cast receivers
    that grey-screen on ultra-short HLS. HLS remains available for lab/fallback.
    """

    protocol_version = "HTTP/1.0"

    def do_OPTIONS(self):
        self.send_response(200, "ok")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def _authorized_resource(self):
        requested = urlsplit(self.path).path.lstrip("/").split("/", 1)
        if len(requested) != 2 or requested[0] != SESSION_TOKEN:
            return None
        return requested[1]

    def do_GET(self):
        resource = self._authorized_resource()
        if resource is None:
            self.send_error(404)
            return
        query = urlsplit(self.path).query
        probe = "probe=1" in query.split("&")
        if resource in {"", "index.html", "player"}:
            body = HTML_PLAYER.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
            return

        path = os.path.join(MEDIA_DIR, resource)
        if not os.path.isfile(path):
            self.send_error(404)
            return

        if not self.client_address[0].startswith("127."):
            with REMOTE_MEDIA_LOCK:
                REMOTE_MEDIA_CLIENTS.add(self.client_address[0])

        if (resource == "live.mp4" or resource.endswith(".mp4")) and not probe:
            self._stream_growing_fmp4(path)
            return

        # HLS playlist/segments, health.json, or fMP4 probe snapshot.
        try:
            with open(path, "rb") as handle:
                data = handle.read(256 * 1024) if probe else handle.read()
        except OSError:
            self.send_error(404)
            return
        content_type = "application/octet-stream"
        if resource.endswith(".m3u8"):
            content_type = "application/vnd.apple.mpegurl"
        elif resource.endswith(".ts"):
            content_type = "video/MP2T"
        elif resource.endswith(".m4s"):
            content_type = "video/iso.segment"
        elif resource.endswith(".mp4"):
            content_type = "video/mp4"
        elif resource.endswith(".json"):
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def _stream_growing_fmp4(self, path: str):
        """Stream a fragmented MP4 that ffmpeg is still appending to."""
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Connection", "close")
        self.end_headers()
        position = 0
        idle_ticks = 0
        while idle_ticks < 120:
            try:
                size = os.path.getsize(path)
            except OSError:
                break
            if size > position:
                try:
                    with open(path, "rb") as handle:
                        handle.seek(position)
                        # Small chunks so the receiver sees new media sooner.
                        chunk = handle.read(min(32 * 1024, size - position))
                except OSError:
                    break
                if not chunk:
                    idle_ticks += 1
                    time.sleep(0.1)
                    continue
                position += len(chunk)
                idle_ticks = 0
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            else:
                idle_ticks += 1
                time.sleep(0.15)

    def log_message(self, fmt, *args):
        pass


class ReuseServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

def parse_ffmpeg_progress() -> dict:
    try:
        with open(PROGRESS_FILE, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return {}

    progress = {}
    for line in lines:
        key, separator, value = line.strip().partition("=")
        if separator:
            progress[key] = value

    def as_number(name: str):
        if name not in progress:
            return None
        try:
            return float(progress[name])
        except (TypeError, ValueError):
            return None

    bitrate = progress.get("bitrate", "").removesuffix("kbits/s")
    frame = as_number("frame")
    dropped = as_number("drop_frames")
    return {
        "frame": int(frame) if frame is not None else None,
        "fps": as_number("fps"),
        "bitrate_kbps": as_number_from_string(bitrate),
        "drop_frames": int(dropped) if dropped is not None else None,
    }

def as_number_from_string(value: str) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def wait_for_stream(wfr_proc, ffm_proc):
    """Publish stream health and real ffmpeg progress while the pipeline runs."""
    stream_ready = False
    lab_media = os.environ.get("NEARBY_CAST_LAB_MEDIA", "").strip().lower() in {"1", "true", "yes"}
    while True:
        metrics = parse_ffmpeg_progress()
        metrics["encoder"] = SELECTED_ENCODER
        metrics["transport"] = TRANSPORT
        if ffm_proc.poll() is not None:
            write_status("failed", "ffmpeg exited before the cast stopped", metrics=metrics)
            return
        if not lab_media and wfr_proc is not ffm_proc and wfr_proc.poll() is not None:
            write_status("failed", "wf-recorder exited before the cast stopped", metrics=metrics)
            return

        if TRANSPORT == "fmp4":
            try:
                stream_ready = os.path.isfile(FMP4_PATH) and os.path.getsize(FMP4_PATH) > 32_000
            except OSError:
                stream_ready = False
        else:
            if not stream_ready and os.path.exists(HLS_PATH):
                try:
                    with open(HLS_PATH, encoding="utf-8") as handle:
                        stream_ready = ".ts" in handle.read() or ".m4s" in handle.read()
                except OSError:
                    pass

        if stream_ready:
            try:
                with open(STATUS_FILE, encoding="utf-8") as handle:
                    current_state = json.load(handle).get("state")
            except (OSError, json.JSONDecodeError):
                current_state = None
            state = current_state if current_state in ("casting", "failed") else "stream_ready"
            message = (
                "Fragmented MP4 media is growing"
                if TRANSPORT == "fmp4"
                else "HLS manifest contains media segments"
            )
            write_status(state, message, metrics=metrics)
        else:
            write_status("starting", "Waiting for captured video frames", metrics=metrics)
        time.sleep(1)

def encoder_video_args_for_cast(base_video_args: list[str]) -> list[str]:
    """Tune encoder args for Cast receivers (baseline + no B-frames)."""
    args = list(base_video_args)
    # Prefer Cast-friendly baseline/level when using libx264.
    if "-c:v" in args and "libx264" in args:
        # Replace profile/level if present; otherwise append.
        def set_flag(flag: str, value: str):
            if flag in args:
                args[args.index(flag) + 1] = value
            else:
                args.extend([flag, value])

        set_flag("-profile:v", "baseline")
        set_flag("-level", "4.0")
        set_flag("-bf", "0")
        set_flag("-g", "15")
        set_flag("-keyint_min", "15")
        set_flag("-bufsize", "700k")
    return args


def media_output_args() -> list[str]:
    if TRANSPORT == "fmp4":
        # Short fragments + flush: still often 10s+ on Default Media Receiver,
        # but much better than 1s keyframe-only fragments with a large VBV.
        return [
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset",
            "-frag_duration",
            "500000",
            "-flush_packets",
            "1",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-f",
            "mp4",
            FMP4_PATH,
        ]
    # Default: 1s HLS — typical Cast live-edge latency ~3–5s (not 15–20s fMP4).
    # Avoid sub-second HLS; that previously grey-screened Android TV boxes.
    return [
        "-f",
        "hls",
        "-hls_time",
        "1",
        "-hls_list_size",
        "3",
        "-hls_flags",
        "delete_segments+append_list+omit_endlist+independent_segments+program_date_time",
        HLS_PATH,
    ]


def start_capture(output_name: str, geometry: str, audio_mode: str):
    """
    Starts wf-recorder | ffmpeg pipeline.
    Returns (wfr_proc, ffm_proc).
    """
    shutil.rmtree(MEDIA_DIR, ignore_errors=True)
    os.makedirs(MEDIA_DIR, exist_ok=True)
    env = make_env()

    log_wfr = open(WFR_LOG, "w")
    log_ffm = open(FFM_LOG, "w")

    # Fresh pipe
    pipe = os.path.join(SESSION_DIR, "capture.raw.pipe")
    if os.path.exists(pipe):
        os.remove(pipe)
    os.mkfifo(pipe)

    # Determine wf-recorder geometry flag (for window capture) or output flag (monitor)
    wfr_geom = parse_window_geometry(geometry)
    # Determine actual capture resolution for ffmpeg
    if wfr_geom:
        # Extract WxH from geometry string
        m = re.search(r'(\d+),(\d+)\s+(\d+)x(\d+)', wfr_geom)
        if m:
            cap_w, cap_h = int(m.group(3)), int(m.group(4))
        else:
            raise RuntimeError("Could not determine selected region resolution")
    else:
        monitor_resolution = get_monitor_resolution(output_name)
        if not monitor_resolution:
            raise RuntimeError("Could not determine selected output resolution. Select a detected display and try again.")
        cap_w, cap_h = monitor_resolution

    # Audio
    if audio_mode == "system":
        audio_in = ["-f", "pulse", "-i", "default.monitor"]
    else:
        audio_in = ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

    print(f"[CAPTURE] cap={cap_w}x{cap_h} geom='{wfr_geom}' audio={audio_mode} transport={TRANSPORT}", flush=True)

    global SELECTED_ENCODER
    SELECTED_ENCODER, encoder_global_args, encoder_video_args = select_video_encoder()
    encoder_video_args = encoder_video_args_for_cast(encoder_video_args)

    ffm_cmd = [
        "ffmpeg", "-y", *encoder_global_args,
        "-fflags", "nobuffer+flush_packets",
        "-flags", "low_delay",
        "-probesize", "32",
        "-analyzeduration", "0",
        "-f", "rawvideo",
        "-video_size", f"{cap_w}x{cap_h}",
        "-pix_fmt", "bgr0",
        "-framerate", "30",
        "-thread_queue_size", "64",
        "-i", pipe,
        *audio_in,
        "-map", "0:v:0", "-map", "1:a:0",
        *encoder_video_args,
        "-acodec", "aac",
        "-b:a", "96k",
        "-ar", "44100",
        "-ac", "2",
        "-flush_packets", "1",
        "-progress", PROGRESS_FILE,
        "-nostats",
        *media_output_args(),
    ]

    # ── wf-recorder: captures Wayland screen → rawvideo bgr0 → pipe ──
    wfr_flags = []
    if wfr_geom:
        wfr_flags += ["-g", wfr_geom]
    elif output_name and output_name.upper() != "PORTAL":
        wfr_flags += ["-o", output_name]

    wfr_cmd = [
        "wf-recorder",
        *wfr_flags,
        "-r", "30",
        "--no-damage",
        "--muxer=rawvideo",
        "-c", "rawvideo",
        "-x", "bgr0",
        "-f", pipe,
    ]
    print(f"[WFR] {' '.join(wfr_cmd)}", flush=True)
    wfr_proc = subprocess.Popen(
        wfr_cmd,
        stdout=log_wfr, stderr=log_wfr,
        stdin=subprocess.PIPE, env=env
    )
    # Answer any "Overwrite?" prompt
    try:
        wfr_proc.stdin.write(b"y\n")
        wfr_proc.stdin.flush()
        wfr_proc.stdin.close()
    except Exception:
        pass

    print(f"[FFM] {' '.join(ffm_cmd)}", flush=True)
    ffm_proc = subprocess.Popen(ffm_cmd, stdout=log_ffm, stderr=log_ffm, env=env)
    write_json(PIDS_FILE, [wfr_proc.pid, ffm_proc.pid])

    return wfr_proc, ffm_proc

def cast_to_tv(media_url: str, target_ip: str) -> bool:
    ready_path = FMP4_PATH if TRANSPORT == "fmp4" else HLS_PATH
    print(f"[CAST] Waiting for media at {ready_path} (transport={TRANSPORT})...", flush=True)
    for attempt in range(40):
        if TRANSPORT == "fmp4":
            try:
                if os.path.isfile(ready_path) and os.path.getsize(ready_path) > 32_000:
                    print(f"[CAST] fMP4 ready after {attempt*0.5:.1f}s ({os.path.getsize(ready_path)} bytes)", flush=True)
                    break
            except OSError:
                pass
        else:
            if os.path.exists(ready_path):
                content = open(ready_path, encoding="utf-8").read()
                if ".ts" in content or ".m4s" in content:
                    print(f"[CAST] HLS ready after {attempt*0.5:.1f}s", flush=True)
                    break
        time.sleep(0.5)
    else:
        write_status("failed", "Media did not become ready in time")
        return False

    # Do not GET live.mp4 here: the growing-file handler holds the connection
    # open for the lifetime of the cast. Probe a tiny static health document.
    try:
        write_json(os.path.join(MEDIA_DIR, "health.json"), {"ok": True, "transport": TRANSPORT})
        health_url = media_url.rsplit("/", 1)[0] + "/health.json"
        with urllib.request.urlopen(health_url, timeout=3) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            response.read(256)
        print(f"[HTTP] Local media URL is reachable: {media_url}", flush=True)
    except Exception as error:
        write_status("failed", f"Local media server is not reachable: {error}")
        return False

    lab_load = os.environ.get("NEARBY_CAST_LAB_CAST_LOAD", "").strip()
    if lab_load:
        try:
            req = urllib.request.Request(
                lab_load,
                data=json.dumps({"url": media_url, "transport": TRANSPORT}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as response:
                payload = json.loads(response.read().decode() or "{}")
            if not payload.get("ok"):
                write_status("failed", f"Virtual Cast load rejected: {payload}")
                return False
            write_status(
                "casting",
                f"Virtual Google Cast receiver accepted the live {TRANSPORT} stream",
                metrics={**parse_ffmpeg_progress(), "encoder": SELECTED_ENCODER, "transport": TRANSPORT},
            )
            return True
        except Exception as error:
            write_status("failed", f"Virtual Cast load failed: {error}")
            return False

    try:
        import pychromecast
        casts, browser = pychromecast.get_chromecasts(known_hosts=[target_ip])
        if not casts:
            write_status("failed", f"No Google Cast receiver found at {target_ip}")
            pychromecast.discovery.stop_discovery(browser)
            return False

        cast = casts[0]
        print(f"[CAST] Found: {cast.name}", flush=True)
        cast.wait(timeout=10)

        try:
            cast.set_volume(0.8)
        except Exception:
            pass

        mc = cast.media_controller
        content_type = "video/mp4" if TRANSPORT == "fmp4" else "application/vnd.apple.mpegurl"
        print(f"[CAST] play_media ({content_type}) → {media_url}", flush=True)
        mc.play_media(
            media_url,
            content_type=content_type,
            title="Nearby Cast Screen",
            stream_type="LIVE",
            autoplay=True,
        )
        try:
            mc.block_until_active(timeout=10)
        except Exception as error:
            print(f"[CAST] block_until_active: {error}", flush=True)

        # Evidence gate: require the receiver to request media AND reach PLAYING.
        # IDLE/BUFFERING alone previously produced grey-screen "Connected" claims.
        deadline = time.monotonic() + 25
        saw_remote_get = False
        playing_ticks = 0
        last_state = None
        while time.monotonic() < deadline:
            with REMOTE_MEDIA_LOCK:
                saw_remote_get = target_ip in REMOTE_MEDIA_CLIENTS or saw_remote_get
            try:
                last_state = mc.status.player_state
            except Exception:
                last_state = None
            print(f"[CAST] player_state={last_state} remote_get={saw_remote_get}", flush=True)
            if last_state == "PLAYING":
                playing_ticks += 1
                if playing_ticks >= 2 and saw_remote_get:
                    break
            elif last_state == "IDLE" and getattr(mc.status, "idle_reason", None):
                write_status(
                    "failed",
                    f"Google Cast receiver idle: {mc.status.idle_reason}",
                    metrics=parse_ffmpeg_progress(),
                )
                pychromecast.discovery.stop_discovery(browser)
                return False
            else:
                playing_ticks = 0
            time.sleep(0.5)
        else:
            write_status(
                "failed",
                f"{target_ip} did not reach PLAYING (last={last_state}, fetched={saw_remote_get}); "
                "receiver may show a grey screen with the previous HLS path",
                metrics=parse_ffmpeg_progress(),
            )
            pychromecast.discovery.stop_discovery(browser)
            return False

        write_status(
            "casting",
            f"Google Cast receiver is playing ({last_state}) via {TRANSPORT}",
            metrics={**parse_ffmpeg_progress(), "encoder": SELECTED_ENCODER, "transport": TRANSPORT},
        )
        pychromecast.discovery.stop_discovery(browser)
        return True
    except Exception as e:
        write_status("failed", f"Google Cast playback failed: {e}")
        return False

def lab_mode_enabled() -> bool:
    for key in ("NEARBY_CAST_VIRTUAL_LAB", "NEARBY_CAST_ALLOW_LOOPBACK", "NEARBY_CAST_LAB_MEDIA"):
        if os.environ.get(key, "").strip().lower() in {"1", "true", "yes"}:
            return True
    return False


def start_lab_capture(audio_mode: str):
    """Synthetic capture for virtual-receiver CI without Wayland."""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    log_ffm = open(FFM_LOG, "w")
    global SELECTED_ENCODER
    SELECTED_ENCODER, encoder_global_args, encoder_video_args = select_video_encoder()
    encoder_video_args = encoder_video_args_for_cast(encoder_video_args)
    if audio_mode == "system":
        audio_in = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
    else:
        audio_in = ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    ffm_cmd = [
        "ffmpeg", "-y", *encoder_global_args,
        "-f", "lavfi", "-re", "-i", "testsrc=size=1280x720:rate=30",
        *audio_in,
        "-map", "0:v:0", "-map", "1:a:0",
        *encoder_video_args,
        "-acodec", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-ac", "2",
        "-shortest",
        "-progress", PROGRESS_FILE,
        "-nostats",
        *media_output_args(),
    ]
    print(f"[LAB] {' '.join(ffm_cmd)}", flush=True)
    ffm_proc = subprocess.Popen(ffm_cmd, stdout=log_ffm, stderr=log_ffm)
    write_json(PIDS_FILE, [ffm_proc.pid])
    # Compatibility with stop() which expects (wfr, ffm)
    return ffm_proc, ffm_proc


def main():
    if len(sys.argv) < 2:
        print("Usage: cast_launcher.py <TARGET_IP> [OUTPUT] [GEOMETRY] [AUDIO]")
        sys.exit(1)

    target_ip   = sys.argv[1]
    output_name = sys.argv[2] if len(sys.argv) > 2 else "PORTAL"
    geometry    = sys.argv[3] if len(sys.argv) > 3 else "PORTAL"
    audio_mode  = sys.argv[4] if len(sys.argv) > 4 else "system"

    local_ip = get_local_ip(target_ip)
    if not local_ip:
        write_status(
            "failed",
            f"Could not determine a route to Google Cast receiver {target_ip}",
        )
        sys.exit(1)
    if local_ip.startswith("127.") and not lab_mode_enabled():
        write_status(
            "failed",
            f"Could not determine a non-loopback route to Google Cast receiver {target_ip}",
        )
        sys.exit(1)
    cleanup()
    write_status("starting", "Preparing screen capture")

    try:
        if os.environ.get("NEARBY_CAST_LAB_MEDIA", "").strip().lower() in {"1", "true", "yes"}:
            wfr_proc, ffm_proc = start_lab_capture(audio_mode)
        else:
            wfr_proc, ffm_proc = start_capture(output_name, geometry, audio_mode)
    except Exception as error:
        write_status("failed", f"Could not start screen capture: {error}")
        print(f"[ERROR] Could not start screen capture: {error}", flush=True)
        sys.exit(1)

    try:
        server = ReuseServer(("0.0.0.0", 0), UniversalHandler)
    except OSError as error:
        write_status("failed", f"Could not start local media server: {error}")
        wfr_proc.terminate()
        ffm_proc.terminate()
        cleanup()
        sys.exit(1)
    http_port = server.server_address[1]
    media_name = "live.mp4" if TRANSPORT == "fmp4" else "live.m3u8"
    media_url = f"http://{local_ip}:{http_port}/{SESSION_TOKEN}/{media_name}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[HTTP] Serving session-authorized {TRANSPORT} on port {http_port}", flush=True)

    def stop(sig=None, frame=None, preserve_status: bool = False):
        try:
            stop_processes(wfr_proc, ffm_proc)
            server.shutdown()
        except Exception:
            pass
        cleanup(remove_status=not preserve_status)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    threading.Thread(target=wait_for_stream, args=(wfr_proc, ffm_proc), daemon=True).start()
    if not cast_to_tv(media_url, target_ip):
        stop(preserve_status=True)
        return

    # Supervise both children. If either side of the pipeline exits, stop the
    # other side as well; otherwise a FIFO writer/reader can leave the process
    # and the UI waiting forever after an encoder or capture failure.
    try:
        while wfr_proc.poll() is None and ffm_proc.poll() is None:
            time.sleep(0.2)
        if wfr_proc.poll() is None or ffm_proc.poll() is None:
            write_status(
                "failed",
                "Capture or encoder process exited unexpectedly",
                metrics={**parse_ffmpeg_progress(), "encoder": SELECTED_ENCODER, "transport": TRANSPORT},
            )
        stop(preserve_status=True)
    except KeyboardInterrupt:
        stop()

if __name__ == "__main__":
    main()
