# Testing

## Automated

```bash
npm run typecheck
npm test
cargo fmt --check --manifest-path src-tauri/Cargo.toml
cargo clippy --manifest-path src-tauri/Cargo.toml -- -D warnings
cargo test --manifest-path src-tauri/Cargo.toml
python3 -m py_compile cast_launcher.py
npm run test:protocols
npm run test:failure
npm run test:latency
```

`npm test` runs Vitest against `src/` only (`.flatpak-builder/` trees are excluded).

## Virtual receivers

```bash
npm run virtual-receivers
# dashboard http://127.0.0.1:8765/ui
```

Evidence boundary for virtual suites:

* NearbyCast — pair/auth/media simulator
* Miracast — lab RTSP/RTP only; physical Wi-Fi Direct **NOT VERIFIED** unless a WFD peer is present
* AirPlay — lab PIN + RTSP; FairPlay Apple TV **NOT VERIFIED** (receiver returns 501)
* Google Cast virtual — DIAL/HLS/load lab; physical PLAYING must be probed separately

## Physical Google Cast / Android TV

```bash
SESSION=$(mktemp -d /tmp/nearby-cast-XXXXXX)
chmod 700 "$SESSION"
export NEARBY_CAST_SESSION_DIR="$SESSION"
export NEARBY_CAST_SESSION_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
export NEARBY_CAST_LAB_MEDIA=1   # optional synthetic frames
python3 cast_launcher.py <RECEIVER_IP> PORTAL PORTAL silent
# Expect player_state PLAYING and status casting via fmp4
```

For real desktop capture, omit `NEARBY_CAST_LAB_MEDIA` and run the Tauri app or Flatpak.

## Latency methodology

1. Display a millisecond clock on the sender screen.
2. Photograph sender + receiver with one camera.
3. Subtract the two displayed times.
4. Record resolution, FPS, encoder, protocol, and network.

Do not invent latency numbers in the UI; show measured ffmpeg metrics only
(or an explicit unavailable / N/A state).

## Packaging smoke

```bash
flatpak run io.nearbycast.NearbyCast
./src-tauri/target/release/nearby-cast
flatpak info --show-permissions io.nearbycast.NearbyCast
```
