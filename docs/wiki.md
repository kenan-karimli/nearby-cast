<html><head></head><body><h1>Nearby Cast — Project Wiki</h1>
<blockquote>
<p><strong>Cast anything. Nearby.</strong> — Open-source Linux desktop screen casting app.</p>
</blockquote>
<hr>
<h2>Table of Contents</h2>
<ol>
<li><a href="#overview">Overview</a></li>
<li><a href="#architecture">Architecture</a></li>
<li><a href="#tech-stack">Tech Stack</a></li>
<li><a href="#core-components">Core Components</a></li>
<li><a href="#streaming-pipeline">Streaming Pipeline</a></li>
<li><a href="#video-encoding-strategy">Video Encoding Strategy</a></li>
<li><a href="#transport-modes">Transport Modes</a></li>
<li><a href="#security-model">Security Model</a></li>
<li><a href="#installation">Installation</a></li>
<li><a href="#development">Development</a></li>
<li><a href="#test-infrastructure">Test Infrastructure</a></li>
<li><a href="#supported-devices">Supported Devices</a></li>
<li><a href="#system-requirements">System Requirements</a></li>
<li><a href="#environment-variables">Environment Variables</a></li>
<li><a href="#license">License</a></li>
</ol>
<hr>
<h2>Overview</h2>
<p><strong>Nearby Cast</strong> is a lightweight, open-source Linux desktop application that streams your screen to compatible devices on your local network — Google Cast / Chromecast and Android TV. Device discovery is handled via <strong>mDNS</strong>, requiring zero manual configuration.</p>
<p><strong>Feature Summary:</strong></p>

Feature | Support
-- | --
Automatic device discovery | ✅ mDNS
Google Cast / Chromecast | ✅
Android TV | ✅
Local network casting | ✅
Wayland | ✅
X11 | ✅
Open source | ✅ MIT


<hr>
<h2>License</h2>
<p>Nearby Cast is released under the <strong>MIT License</strong>. See <a href="https://github.com/kenan-karimli/nearby-cast/blob/main/LICENSE"><code>LICENSE</code></a> for details.</p>
<hr>
<p><em>Last updated: August 2026 | Author: <a href="https://github.com/kenan-karimli">kenan-karimli</a></em></p></body></html># Nearby Cast — Project Wiki

> **Cast anything. Nearby.** — Open-source Linux desktop screen casting app.

---

## Table of Contents

1. [[Overview](https://github.com/kenan-karimli/nearby-cast/wiki/_new#overview)](#overview)
2. [[Architecture](https://github.com/kenan-karimli/nearby-cast/wiki/_new#architecture)](#architecture)
3. [[Tech Stack](https://github.com/kenan-karimli/nearby-cast/wiki/_new#tech-stack)](#tech-stack)
4. [[Core Components](https://github.com/kenan-karimli/nearby-cast/wiki/_new#core-components)](#core-components)
5. [[Streaming Pipeline](https://github.com/kenan-karimli/nearby-cast/wiki/_new#streaming-pipeline)](#streaming-pipeline)
6. [[Video Encoding Strategy](https://github.com/kenan-karimli/nearby-cast/wiki/_new#video-encoding-strategy)](#video-encoding-strategy)
7. [[Transport Modes](https://github.com/kenan-karimli/nearby-cast/wiki/_new#transport-modes)](#transport-modes)
8. [[Security Model](https://github.com/kenan-karimli/nearby-cast/wiki/_new#security-model)](#security-model)
9. [[Installation](https://github.com/kenan-karimli/nearby-cast/wiki/_new#installation)](#installation)
10. [[Development](https://github.com/kenan-karimli/nearby-cast/wiki/_new#development)](#development)
11. [[Test Infrastructure](https://github.com/kenan-karimli/nearby-cast/wiki/_new#test-infrastructure)](#test-infrastructure)
12. [[Supported Devices](https://github.com/kenan-karimli/nearby-cast/wiki/_new#supported-devices)](#supported-devices)
13. [[System Requirements](https://github.com/kenan-karimli/nearby-cast/wiki/_new#system-requirements)](#system-requirements)
14. [[Environment Variables](https://github.com/kenan-karimli/nearby-cast/wiki/_new#environment-variables)](#environment-variables)
15. [[License](https://github.com/kenan-karimli/nearby-cast/wiki/_new#license)](#license)

---

## Overview

**Nearby Cast** is a lightweight, open-source Linux desktop application that streams your screen to compatible devices on your local network — Google Cast / Chromecast and Android TV. Device discovery is handled via **mDNS**, requiring zero manual configuration.

**Feature Summary:**

| Feature | Support |
|---|---|
| Automatic device discovery | ✅ mDNS |
| Google Cast / Chromecast | ✅ |
| Android TV | ✅ |
| Local network casting | ✅ |
| Wayland | ✅ |
| X11 | ✅ |
| Open source | ✅ MIT |

---

## Architecture

The project is split into two main layers:

```
┌─────────────────────────────────────────────┐
│              Tauri Desktop App               │
│         (React + TypeScript frontend)        │
│                                              │
│  - mDNS device discovery                     │
│  - User interface                            │
│  - Manages cast_launcher.py subprocess       │
└──────────────────┬──────────────────────────┘
                   │ subprocess
                   ▼
┌─────────────────────────────────────────────┐
│          cast_launcher.py (Python)           │
│                                             │
│  wf-recorder → FIFO pipe → ffmpeg           │
│       │                       │             │
│  (Wayland capture)     (H.264 encode)       │
│                               │             │
│                    HTTP Media Server        │
│                  (fMP4 / HLS stream)        │
│                               │             │
│                    Chromecast / ATV         │
└─────────────────────────────────────────────┘
```

**Inter-component communication:**
- The Tauri frontend spawns `cast_launcher.py` as a child process
- The Python script is authorized via `SESSION_DIR` and `SESSION_TOKEN` environment variables
- Each session gets an isolated directory with `0700` permissions
- Status changes are communicated back to the frontend by polling `status.json`

---

## Tech Stack

### Frontend
| Technology | Version | Purpose |
|---|---|---|
| **Tauri** | v2.x | Lightweight Rust-based desktop framework |
| **React** | ^19.0.0 | UI components |
| **TypeScript** | ^5.7.3 | Type safety |
| **Vite** | ^6.1.0 | Build tool |
| **Lucide React** | ^0.475.0 | Icon library |

### Backend / Capture Pipeline
| Technology | Version | Purpose |
|---|---|---|
| **Python 3** | 3.x | Streaming orchestration |
| **wf-recorder** | System | Wayland screen capture |
| **ffmpeg** | System | Video encoding |
| **pychromecast** | pip | Google Cast protocol |

### Packaging
| Format | Target |
|---|---|
| `.deb` | Debian / Ubuntu |
| `.rpm` | Fedora / RHEL |
| `.bin` | Universal binary |
| **Flatpak** | Sandboxed install |
| **Snap** | Ubuntu Snap Store |

---

## Core Components

### 1. `cast_launcher.py` — Streaming Orchestrator

This ~974-line Python script manages the entire capture → encode → serve → cast lifecycle.

**Key functions:**

- **`select_video_encoder()`** — probes available hardware/software encoders and picks the best one (`h264_vaapi` → `h264_nvenc` → `h264_qsv` → `libx264`)
- **`start_capture()`** — starts the `wf-recorder | ffmpeg` pipeline, passing raw video through a FIFO pipe
- **`cast_to_tv()`** — connects to the Cast receiver via `pychromecast` and instructs it to load the media URL
- **`UniversalHandler`** — session-token-gated HTTP media server; implements growing-file streaming for fMP4
- **`wait_for_stream()`** — reads ffmpeg progress output and updates the status JSON in real time
- **`get_monitor_resolution()`** — detects monitor resolution via `wlr-randr`, `hyprctl`, or `xrandr`

### 2. Tauri App (`src-tauri/`)

The Rust-based desktop shell:
- Discovers Cast devices on the local network via mDNS
- Manages the Python backend as a subprocess
- Generates `session_dir` and `session_token` and passes them to `cast_launcher.py`

### 3. React Frontend (`src/`)

- Lists discovered devices for the user to select
- Handles device selection, monitor/window selection, and audio mode
- Polls `status.json` to display live cast state

### 4. Embedded HTML Player

The `HTML_PLAYER` template inside `cast_launcher.py`:
- Plays HLS streams using the `hls.js` library
- Sets `<video src>` directly for fMP4 transport
- Transport is selected via the URL parameter `?src=live.mp4` or `?src=live.m3u8`

---

## Streaming Pipeline

```
Monitor / Window
      │
      ▼
wf-recorder (Wayland)
  --muxer=rawvideo -c rawvideo -x bgr0 -r 30
      │
   FIFO pipe (capture.raw.pipe)
      │
      ▼
ffmpeg
  -f rawvideo -pix_fmt bgr0 -framerate 30
  + audio (PulseAudio monitor / silence)
  → H.264 encode (VA-API / NVENC / QSV / libx264)
  → fMP4 (live.mp4)  or  HLS (live.m3u8 + .ts segments)
      │
      ▼
HTTP Media Server (0.0.0.0 : random port)
  Session-token-protected URL
      │
      ▼
pychromecast → Cast receiver
  play_media(URL, content_type, stream_type="LIVE")
```

**Latency tuning:**
- GOP: 15 frames @ 30fps = **0.5 s** keyframe interval
- `frag_duration=500000` (0.5 s fragments)
- `-fflags nobuffer+flush_packets -flags low_delay`
- `probesize=32`, `analyzeduration=0`

---

## Video Encoding Strategy

Encoder selection uses an automatic probe mechanism — a real one-frame encode is attempted for each candidate before it is accepted:

```
Priority order:
1. h264_vaapi   (VA-API — Intel / AMD GPU, per /dev/dri/renderD*)
2. h264_nvenc   (NVIDIA GPU)
3. h264_qsv     (Intel Quick Sync)
4. libx264      (Software fallback — always available)
```

**Cast-optimized encoding parameters:**
- H.264 Baseline Profile, Level 4.0 (Cast receiver compatibility)
- No B-frames (`-bf 0`) — reduces latency
- Bitrate: ~4 Mbps video + 96 kbps AAC audio
- The `NEARBY_CAST_ENCODER` env var forces a specific encoder

---

## Transport Modes

Selected via the `NEARBY_CAST_TRANSPORT` environment variable:

### fMP4 (Fragmented MP4) — Default
```
NEARBY_CAST_TRANSPORT=fmp4  (default)
```
- **Verified** working on Android TV Cast receivers
- Growing-file HTTP streaming: the server reads and forwards data as ffmpeg writes it
- Typical latency: 10–20 s (Default Media Receiver limitation)
- Content-Type: `video/mp4`

### HLS (HTTP Live Streaming)
```
NEARBY_CAST_TRANSPORT=hls
```
- Typical latency: ~3–5 s
- Faster when the receiver accepts it
- 1-second segments, 3-segment playlist
- Known to grey-screen some Android TV boxes — fMP4 is the safer default

---

## Security Model

The project uses multiple layers of isolation:

**Session isolation:**
- Each cast session gets a unique `SESSION_DIR` with mode `0700`
- All child resources live under this directory
- Concurrent or previously failed sessions cannot access each other's files

**Media server authorization:**
- The HTTP URL embeds a session token: `/{SESSION_TOKEN}/live.mp4`
- Requests without a valid token receive a `404`
- The token is generated by the Tauri process and passed via environment variable

**Process management:**
- Child PIDs (`wf-recorder`, `ffmpeg`) are saved in `children.json`
- `SIGTERM` / `SIGINT` handlers gracefully stop both processes and clean up temp files
- If either side of the pipeline exits unexpectedly, the other is terminated immediately

---

## Installation

### Quick Install (one command)
```bash
curl -fsSL https://raw.githubusercontent.com/kenan-karimli/nearby-cast/main/install.sh | sh
```
The installer auto-detects your Linux distribution and installs the appropriate package.

### Manual Install

Download the latest release from [[GitHub Releases](https://github.com/kenan-karimli/nearby-cast/releases/latest)](https://github.com/kenan-karimli/nearby-cast/releases/latest):

| Package | System |
|---|---|
| `.deb` | Debian, Ubuntu, Linux Mint |
| `.rpm` | Fedora, openSUSE, RHEL |
| `.bin` | Any Linux |

### System Dependencies

The cast launcher requires the following to be installed:
- `wf-recorder` — Wayland screen capture
- `ffmpeg` — video encoding
- `python3` — runtime for `cast_launcher.py`
- `python3-pychromecast` — Google Cast protocol

---

## Development

```bash
git clone https://github.com/kenan-karimli/nearby-cast.git
cd nearby-cast
npm install
npm run dev      # Vite dev server
npm run tauri    # Tauri development build
```

### Build

```bash
npm run build    # TypeScript + Vite production build
```

### NPM Scripts Reference

| Script | Description |
|---|---|
| `npm run dev` | Start Vite dev server |
| `npm run build` | Production build |
| `npm run typecheck` | TypeScript type check (no emit) |
| `npm run tauri` | Tauri CLI |
| `npm run virtual-receivers` | Start virtual Cast receiver server |
| `npm run test:virtual` | E2E test against virtual receivers |
| `npm run test:protocols` | E2E + production path tests |
| `npm run test:latency` | Measure stream latency |
| `npm run test:stress` | Stability under load |
| `npm run test:all` | Run every test suite |

---

## Test Infrastructure

The `tools/virtual-receivers/` directory contains a comprehensive test suite that allows testing the full pipeline without real Cast hardware:

| Script | Purpose |
|---|---|
| `e2e_test.py` | Full end-to-end cast flow |
| `production_path_test.py` | Production pipeline validation |
| `failure_test.py` | Error and failure scenario handling |
| `latency_test.py` | Stream latency measurement |
| `stress_test.py` | Stability under sustained load |
| `run_all_tests.py` | Sequential runner for all suites |

**Lab mode** (`NEARBY_CAST_LAB_MEDIA=1`):
- Enables testing in CI environments without Wayland
- Uses `ffmpeg lavfi testsrc` instead of `wf-recorder`
- Posts the stream URL to a virtual receiver HTTP endpoint (`NEARBY_CAST_LAB_CAST_LOAD`)

---

## Supported Devices

| Device | Protocol | Status |
|---|---|---|
| Google Chromecast | Google Cast | ✅ Supported |
| Chromecast with Google TV | Google Cast | ✅ Supported |
| Android TV | Google Cast / fMP4 LIVE | ✅ Verified |
| Google Nest Hub | Google Cast | ✅ Supported |
| Cast-enabled smart TVs | Google Cast | ✅ Supported |

---

## System Requirements

| Requirement | Details |
|---|---|
| OS | Linux |
| Display server | Wayland recommended (wlroots-compatible: Sway, Hyprland, GNOME Wayland); X11 also supported |
| Network | Same local network as the Cast device |
| GPU / CPU | Any; hardware encoder strongly recommended for performance |

**Wayland compositor detection** (in `get_monitor_resolution`):
- `wlr-randr` — wlroots-based compositors
- `hyprctl` — Hyprland
- `xrandr` — X11 / XWayland fallback

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `NEARBY_CAST_SESSION_DIR` | — | **Required.** Path to the session directory |
| `NEARBY_CAST_SESSION_TOKEN` | — | **Required.** HTTP URL authorization token |
| `NEARBY_CAST_TRANSPORT` | `fmp4` | Transport mode: `fmp4` or `hls` |
| `NEARBY_CAST_ENCODER` | `auto` | Force encoder: `auto`, `h264_vaapi`, `h264_nvenc`, `h264_qsv`, `libx264` |
| `NEARBY_CAST_LAB_MEDIA` | — | Set to `1` to enable Wayland-free lab mode |
| `NEARBY_CAST_LAB_CAST_LOAD` | — | Virtual receiver HTTP endpoint for lab casts |
| `NEARBY_CAST_ALLOW_LOOPBACK` | — | Set to `1` to permit casting over loopback |
| `XDG_RUNTIME_DIR` | `/run/user/<uid>` | Wayland runtime directory |
| `WAYLAND_DISPLAY` | `wayland-1` | Wayland display socket |

---

## License

Nearby Cast is released under the **MIT License**. See [`[LICENSE](https://github.com/kenan-karimli/nearby-cast/blob/main/LICENSE)`](https://github.com/kenan-karimli/nearby-cast/blob/main/LICENSE) for details.

---

*Last updated: August 2026 | Author: [[kenan-karimli](https://github.com/kenan-karimli)](https://github.com/kenan-karimli)*
