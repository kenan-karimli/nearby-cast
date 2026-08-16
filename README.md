# Nearby Cast

A lightweight Linux app for discovering and casting to compatible devices on your local network.

<p align="center">
  <img src="src/swappy-20260812_082317.png" width="45%">
  <img src="src/swappy-20260812_082329.png" width="45%">
</p>

## Features

* Automatic device discovery (mDNS + SSDP) with multi-protocol device merging
* Google Cast / Chromecast screen casting via low-latency fragmented MP4
* Miracast / Wi-Fi Direct casting via FluxCast helper
* AirPlay lab mirroring (PIN + RTSP); FairPlay Apple TV not verified
* NearbyCast authenticated sender/receiver with pairing + persistent trust
* Wayland screen capture through `wf-recorder`
* Automatic protocol selection preferring lower-latency transports
* Lightweight Tauri desktop app

## Supported Devices

| Path | Status |
|---|---|
| Google Cast sender | Implemented (fMP4 default; HLS fallback). Physical Android TV Cast verified PLAYING on this project’s test LAN |
| Miracast sender | FluxCast WFD + lab RTSP/RTP; needs NetworkManager P2P for physical displays |
| NearbyCast sender/receiver | Authenticated control + media path; pair on first connect |
| AirPlay sender | Lab mirroring (PIN + RTSP); FairPlay Apple TV auth not verified |

See `docs/CURRENT_STATE.md` for the verified feature matrix and hardware results.
See `docs/PROTOCOLS.md`, `docs/TESTING.md`, and `docs/TROUBLESHOOTING.md`.

## Install

### Quick Install

Install the latest version with one command:

```bash
curl -fsSL https://raw.githubusercontent.com/kenan-karimli/nearby-cast/main/install.sh | sh
```

The installer automatically detects your Linux system and installs the appropriate release.

### Flatpak (recommended on Linux)

From a checkout:

```bash
flatpak-builder --user --force-clean --repo=repo-flatpak \
  build-flatpak flatpak/io.nearbycast.NearbyCast.yml
flatpak --user remote-add --no-gpg-verify --if-not-exists nearbycast-local repo-flatpak
flatpak --user install -y nearbycast-local io.nearbycast.NearbyCast
flatpak run io.nearbycast.NearbyCast
```

Runtime: GNOME Platform 49. See `docs/BUILD_AND_RELEASE.md`.

### Manual Install

Download the latest release:

**[Download Latest Release](https://github.com/kenan-karimli/nearby-cast/releases/latest)**

Available packages may include:

* `.bin`
* `.deb`
* `.rpm`
* Flatpak (local repo / Flathub later)

## Requirements

* Linux
* Local network connection
* A compatible casting device on the same network

## Usage

Launch **Nearby Cast**, wait for nearby devices to be discovered, then select a device to start casting.

* **Google Cast** — Cast to Chromecast-compatible displays on the LAN.
* **Wireless** — Scan for Wi-Fi Direct Miracast displays (requires FluxCast).
* **Allow Projecting to This PC** — Enable the authenticated NearbyCast receiver. When a sender pairs, the six-digit code appears in the receiver card.

For FluxCast in development checkouts, either install `fluxcast` on `PATH`, set `NEARBY_CAST_FLUXCAST`, or use the local `.venv-fluxcast` helper created for this workspace.

## Development

Clone the repository:

```bash
git clone https://github.com/kenan-karimli/nearby-cast.git
cd nearby-cast
```

### Virtual receiver lab (no physical hardware required)

```bash
npm run virtual-receivers          # start Google Cast / Miracast / AirPlay / NearbyCast lab + dashboard
npm run test:virtual               # protocol E2E against virtual endpoints
npm run test:virtual-production    # production launcher/sender paths against virtual endpoints
npm run test:protocols             # both suites
```

Dashboard: `http://127.0.0.1:8765/ui`

See `docs/BUILD_AND_RELEASE.md` for the current development and packaging
commands.


## License

MIT
