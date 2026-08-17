# Nearby Cast

Nearby Cast is a Linux desktop screen-casting application for compatible network displays.

## Supported paths

- Google Cast receivers: screen capture over H.264/fMP4, with HLS fallback.
- NearbyCast receivers: authenticated sender/receiver path.
- Miracast and AirPlay: lab/third-party receiver paths are verified; physical Wi-Fi Direct and FairPlay Apple TV support are not verified.
- Capture sources: full display, selected window, and region/crop on supported Wayland environments.

## Install

Builds are published in GitHub Releases. The native `.deb` and `.rpm` bundles include the production launcher; Google Cast also needs `wf-recorder`, FFmpeg, Python 3, and `pychromecast` on the host.

## Build

```bash
npm ci
npm run build
npm run tauri build
```

For the Rust binary only:

```bash
cargo build --release --manifest-path src-tauri/Cargo.toml
```

## Use

1. Start Nearby Cast on the same LAN as the receiver.
2. Select a discovered receiver and a capture source.
3. Start casting and stop it from the application when finished.

## Test

```bash
npm test
cargo test --manifest-path src-tauri/Cargo.toml
npm run test:all
```

The virtual receiver suite does not replace testing with physical receivers. See [`docs/TESTING.md`](docs/TESTING.md) for protocol-specific limitations.

## License

MIT
