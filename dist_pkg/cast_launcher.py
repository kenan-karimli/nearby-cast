#!/usr/bin/env python3
"""
Nearby Cast — Direct Capture Streaming Launcher

Confirmed working pipeline:
  wf-recorder --muxer=rawvideo -c rawvideo -x bgr0 -f pipe
  | ffmpeg -f rawvideo -video_size WxH -pix_fmt bgr0 -framerate 30 -i pipe
            -> libx264 baseline -> HLS -> HTTP server -> Chromecast
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

HLS_DIR   = "/tmp/nearby_cast_hls"
HTTP_PORT = 8090

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
        const src = '/live.m3u8';
        if (Hls.isSupported()) {
            const hls = new Hls({ maxBufferLength:4, liveSyncDurationCount:1,
                                   liveMaxLatencyDurationCount:3, enableWorker:true });
            hls.loadSource(src);
            hls.attachMedia(video);
            hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(()=>{}));
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
            video.src = src;
            video.addEventListener('loadedmetadata', () => video.play().catch(()=>{}));
        }
    </script>
</body>
</html>
"""

def get_local_ip(target: str) -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def cleanup():
    for pat in ["wf-recorder", "ffmpeg.*nearby_cast"]:
        subprocess.run(["pkill", "-9", "-f", pat], stderr=subprocess.DEVNULL)
    time.sleep(0.2)
    try:
        for f in os.listdir("/tmp"):
            if f.startswith("nearby_raw_"):
                try:
                    os.unlink(f"/tmp/{f}")
                except Exception:
                    pass
    except Exception:
        pass

def make_env():
    env = os.environ.copy()
    if not env.get("XDG_RUNTIME_DIR"):
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    if not env.get("WAYLAND_DISPLAY"):
        env["WAYLAND_DISPLAY"] = "wayland-1"
    return env

def get_monitor_resolution() -> tuple:
    """Returns (width, height) of the primary monitor."""
    try:
        result = subprocess.run(
            ["hyprctl", "monitors", "-j"],
            capture_output=True, text=True, env=make_env(), timeout=3
        )
        monitors = json.loads(result.stdout)
        if monitors:
            m = monitors[0]
            return int(m.get("width", 1920)), int(m.get("height", 1080))
    except Exception:
        pass
    return 1920, 1080

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

class UniversalHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=HLS_DIR, **kwargs)

    def do_OPTIONS(self):
        self.send_response(200, "ok")
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()

    def do_GET(self):
        if self.path in ('/', '/index.html', '/player', '/watch'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(HTML_PLAYER.encode('utf-8'))
            return
        super().do_GET()

    def end_headers(self):
        if self.path.endswith('.m3u8'):
            self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
        elif self.path.endswith('.ts'):
            self.send_header('Content-Type', 'video/MP2T')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        super().end_headers()

    def log_message(self, fmt, *args):
        pass

class ReuseServer(socketserver.TCPServer):
    allow_reuse_address = True

def start_capture(output_name: str, geometry: str, audio_mode: str):
    """
    Starts wf-recorder | ffmpeg pipeline.
    Returns (wfr_proc, ffm_proc).
    """
    os.makedirs(HLS_DIR, exist_ok=True)
    env = make_env()

    log_wfr = open("/tmp/nearby_wfr.log", "w")
    log_ffm = open("/tmp/nearby_ffm.log", "w")

    # Fresh pipe
    pipe = f"/tmp/nearby_raw_{int(time.time())}.pipe"
    if os.path.exists(pipe):
        os.remove(pipe)
    os.mkfifo(pipe)

    # Determine wf-recorder geometry flag (for window capture) or output flag (monitor)
    wfr_geom = parse_window_geometry(geometry)
    mon_w, mon_h = get_monitor_resolution()

    # Determine actual capture resolution for ffmpeg
    if wfr_geom:
        # Extract WxH from geometry string
        m = re.search(r'(\d+),(\d+)\s+(\d+)x(\d+)', wfr_geom)
        if m:
            cap_w, cap_h = int(m.group(3)), int(m.group(4))
        else:
            cap_w, cap_h = mon_w, mon_h
    else:
        cap_w, cap_h = mon_w, mon_h

    # Audio
    if audio_mode == "system":
        audio_in = ["-f", "pulse", "-i", "default.monitor"]
    else:
        audio_in = ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

    print(f"[CAPTURE] cap={cap_w}x{cap_h} geom='{wfr_geom}' audio={audio_mode}", flush=True)

    # ── ffmpeg: reads rawvideo bgr0 pipe → H.264 baseline → HLS ──
    ffm_cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-video_size", f"{cap_w}x{cap_h}",
        "-pix_fmt", "bgr0",
        "-framerate", "30",
        "-i", pipe,
        *audio_in,
        "-map", "0:v:0", "-map", "1:a:0",
        "-vcodec", "libx264",
        "-profile:v", "baseline",
        "-level",    "3.0",
        "-preset",   "ultrafast",
        "-tune",     "zerolatency",
        "-pix_fmt",  "yuv420p",
        "-g",        "30",
        "-keyint_min", "30",
        "-sc_threshold", "0",
        "-acodec",   "aac",
        "-b:a",      "128k",
        "-ar",       "44100",
        "-hls_time", "1",
        "-hls_list_size", "5",
        "-hls_flags", "delete_segments+omit_endlist",
        f"{HLS_DIR}/live.m3u8",
    ]
    print(f"[FFM] {' '.join(ffm_cmd)}", flush=True)
    ffm_proc = subprocess.Popen(ffm_cmd, stdout=log_ffm, stderr=log_ffm, env=env)
    time.sleep(0.2)

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

    return wfr_proc, ffm_proc

def cast_to_tv(hls_url: str, target_ip: str):
    manifest = f"{HLS_DIR}/live.m3u8"
    print(f"[CAST] Waiting for HLS manifest at {manifest}...", flush=True)
    for attempt in range(30):
        if os.path.exists(manifest):
            content = open(manifest).read()
            if ".ts" in content:
                print(f"[CAST] HLS ready after {attempt*0.5:.1f}s", flush=True)
                break
        time.sleep(0.5)
    else:
        print("[CAST] HLS timeout, proceeding anyway.", flush=True)

    try:
        import pychromecast
        casts, browser = pychromecast.get_chromecasts(known_hosts=[target_ip])
        if not casts:
            print(f"[CAST] No Chromecast found at {target_ip}", flush=True)
            pychromecast.discovery.stop_discovery(browser)
            return

        cast = casts[0]
        print(f"[CAST] Found: {cast.name}", flush=True)
        cast.wait(timeout=10)

        try:
            cast.set_volume(0.8)
        except Exception:
            pass

        mc = cast.media_controller
        print(f"[CAST] play_media → {hls_url}", flush=True)
        mc.play_media(
            hls_url,
            content_type="application/x-mpegURL",
            title="Nearby Cast Screen",
            stream_type="LIVE",
            autoplay=True,
        )
        mc.block_until_active(timeout=10)
        print(f"[CAST] player_state={mc.status.player_state}", flush=True)
        pychromecast.discovery.stop_discovery(browser)
    except Exception as e:
        print(f"[CAST_ERROR] {e}", flush=True)

def main():
    if len(sys.argv) < 2:
        print("Usage: cast_launcher.py <TARGET_IP> [OUTPUT] [GEOMETRY] [AUDIO]")
        sys.exit(1)

    target_ip   = sys.argv[1]
    output_name = sys.argv[2] if len(sys.argv) > 2 else "PORTAL"
    geometry    = sys.argv[3] if len(sys.argv) > 3 else "PORTAL"
    audio_mode  = sys.argv[4] if len(sys.argv) > 4 else "system"

    local_ip = get_local_ip(target_ip)
    hls_url  = f"http://{local_ip}:{HTTP_PORT}/live.m3u8"

    cleanup()

    wfr_proc, ffm_proc = start_capture(output_name, geometry, audio_mode)

    server = ReuseServer(("0.0.0.0", HTTP_PORT), UniversalHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[HTTP] Serving at http://{local_ip}:{HTTP_PORT}/", flush=True)

    threading.Thread(target=cast_to_tv, args=(hls_url, target_ip), daemon=True).start()

    def stop(sig=None, frame=None):
        try:
            wfr_proc.terminate()
            ffm_proc.terminate()
            server.shutdown()
        except Exception:
            pass
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        wfr_proc.wait()
    except KeyboardInterrupt:
        stop()

if __name__ == "__main__":
    main()
