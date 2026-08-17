# NearbyCast current-state audit

Updated 2026-08-16 after Flatpak install/launch verification and physical Cast re-probe.

## Execution paths

### Google Cast (default)

```text
UI → Tauri → cast_launcher.py
  → wf-recorder | ffmpeg (or lavfi lab media)
  → fragmented MP4 (HTTP/1.0 growing-file stream, no Content-Length)
  → pychromecast play_media(video/mp4, LIVE)
  → Connected only after remote GET + player_state PLAYING
```

`NEARBY_CAST_TRANSPORT=hls` retains a 1s-segment HLS fallback.

### Miracast / AirPlay / NearbyCast

Unchanged architecture: FluxCast P2P or lab RTSP/RTP; AirPlay lab PIN+RTSP;
NearbyCast auth + token media. See `docs/PROTOCOLS.md`.

## Physical hardware evidence (this LAN)

| Target | Result |
|---|---|
| Android TV / CNTV Plus Smart Box `192.168.0.3` | **PASS** — fMP4 reached sustained `PLAYING` (re-verified 2026-08-16; ports 8008/8009/8443 open; mDNS discovery from Flatpak + native) |
| OneScreen brand label | Not advertised as "OneScreen" on this LAN; Cast box above is the verified Cast receiver |
| Miracast Wi-Fi Direct peers | None found; physical P2P **NOT VERIFIED** |
| AirPlay / Apple TV | **NOT VERIFIED** (FairPlay absent; lab binary pair-setup returns honest 501) |
| Second NearbyCast host | **NOT VERIFIED** (virtual receiver PASS) |

## Packaging evidence

| Artifact | Result |
|---|---|
| Flatpak (`io.nearbycast.NearbyCast`, GNOME Platform **49**) | **PASS** — built, installed from `repo-flatpak`, launched; discovered `Android TV` at `192.168.0.3`; `wf-recorder` 0.6.0 + `codecs-extra` (libx264) |
| Native release binary | **PASS** — `src-tauri/target/release/nearby-cast` starts and discovers Cast |
| deb / rpm | **PARTIAL** — artifacts exist under `src-tauri/target/release/bundle/`; not clean-install verified on this host |
| AppImage | **BLOCKED** / not produced in this run |

Flatpak permissions (current): Wayland, fallback X11, DRI, network, PulseAudio, session-bus, PipeWire socket, ScreenCast/Desktop/Notification/Background portals, StatusNotifierWatcher. Codecs via `codecs-extra` (not broad host filesystem). No extra filesystem grants beyond PipeWire.

## Validation commands run

```bash
npm run typecheck          # PASS
npm test                   # PASS (src only; Flatpak build trees excluded)
cargo fmt --check          # PASS
cargo clippy -D warnings   # PASS
cargo test                 # PASS (23)
python3 -m py_compile cast_launcher.py
npm run test:protocols     # PASS (E2E + production-path)
npm run test:failure       # PASS (incl. FairPlay → 501)
```

Physical Cast probe:

```text
cast_launcher.py 192.168.0.3 … NEARBY_CAST_LAB_MEDIA=1
→ player_state=PLAYING remote_get=True
→ status casting: Google Cast receiver is playing (PLAYING) via fmp4
```

## Feature matrix

| Feature | Status | Evidence |
|---|---|---|
| Device discovery | PASS | mDNS Cast found `Android TV` from Flatpak + native |
| Device merging | PASS | unit test coalesces multi-protocol ads |
| Protocol router | PASS | eligibility requires local sender; Miracast → NearbyCast → AirPlay → Cast ranks |
| Google Cast sender | PASS (physical PLAYING) | fMP4 on CNTV Plus / Android TV |
| Miracast lab | PASS | virtual RTSP/RTP |
| Miracast physical P2P | BLOCKED / NOT VERIFIED | no peers on scan |
| AirPlay lab | PASS | virtual suite |
| AirPlay FairPlay | BLOCKED | honest 501 refuse |
| NearbyCast | PASS (simulator) | virtual + production-path |
| Capture / encoder probe | PASS | real one-frame probes; VAAPI may fall back to libx264 |
| Metrics honesty | PASS | ffmpeg progress only; UI shows N/A / “Latency unavailable” when unknown |
| Flatpak | PASS | GNOME 49; install + launch verified (migrated off EOL 47) |
| deb/rpm | PARTIAL | files present; install not verified here |

## Known limitations

* Google Cast latency is higher than Miracast/NearbyCast by design (HTTP media receiver).
* Miracast needs Wi-Fi Direct capable hardware + FluxCast.
* AirPlay FairPlay not implemented.
* Flatpak python deps module uses build-time network (replace with pip-generator for Flathub).
* Physical OneScreen playback was not available for verification in this audit; do not treat the virtual receiver results as hardware evidence.
