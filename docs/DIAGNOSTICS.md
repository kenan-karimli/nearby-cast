# NearbyCast diagnostics

## Local verification commands

```bash
npm run typecheck
npm test
cargo fmt --check --manifest-path src-tauri/Cargo.toml
cargo clippy --manifest-path src-tauri/Cargo.toml -- -D warnings
cargo test --manifest-path src-tauri/Cargo.toml
python3 -m py_compile cast_launcher.py
npm run test:protocols
```

## Virtual lab

```bash
npm run virtual-receivers
curl -s http://127.0.0.1:8765/status | jq .
```

## Session status

Active casts write `status.json` under a 0700 temp session directory. Health
polling (`check_cast_alive`) reads that file. Google Cast only enters `casting`
after the receiver reaches `PLAYING`.

## Protocol notes

* Google Cast default transport: fragmented MP4 (`NEARBY_CAST_TRANSPORT=fmp4`).
* Miracast IPv4 targets use `tools/virtual-receivers/miracast/lab_sender.py`.
* Miracast MAC targets use FluxCast Wi-Fi Direct.
* AirPlay lab uses PIN + RTSP; FairPlay Apple TV is refused.
* Set `NEARBY_CAST_VIRTUAL_LAB=1` / `NEARBY_CAST_LAB_MEDIA=1` only for lab/CI.

## Logs for hardware bugs

* UI stage (`connecting` / `casting` / `failed`)
* `cast_log` events
* Session `status.json` and ffmpeg progress
* Receiver player state (`PLAYING` vs `BUFFERING`)
