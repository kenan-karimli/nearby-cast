# Nearby Cast

Cast your Linux desktop to nearby TVs.

<p align="center">
  <img src="src/swappy-20260812_082317.png" width="45%">
  <img src="src/swappy-20260812_082329.png" width="45%">
</p>

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/kenan-karimli/nearby-cast/main/install.sh | sh
```

Then run `nearby-cast`.

## Requirements

- Linux (Wayland)
- Same Wi‑Fi/LAN as your TV
- `wf-recorder`, `ffmpeg`, `python3`, `pychromecast`

```bash
# Arch
sudo pacman -S wf-recorder ffmpeg python-pip
pip install --user pychromecast
```

## Usage

1. Open **Nearby Cast**
2. Wait for your TV (e.g. Android TV / Chromecast)
3. Hit **Cast**

## License

MIT
