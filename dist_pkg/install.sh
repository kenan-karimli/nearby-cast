#!/usr/bin/env bash
# Install Nearby Cast from the latest GitHub Release tarball.
set -euo pipefail

REPO="kenan-karimli/nearby-cast"
API="https://api.github.com/repos/${REPO}/releases/latest"
TMP_DIR=$(mktemp -d)
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

echo "Nearby Cast - Installation"
echo "--------------------------"
echo ""

if ! command -v curl >/dev/null || ! command -v python3 >/dev/null || ! command -v tar >/dev/null; then
  echo "Need curl, python3, and tar on PATH." >&2
  exit 1
fi

echo "Resolving latest release..."
ASSET_URL=$(curl -fsSL "$API" | python3 -c '
import json, sys
rel = json.load(sys.stdin)
if rel.get("message"):
    raise SystemExit(rel["message"])
for asset in rel.get("assets") or []:
    name = asset.get("name") or ""
    if name.endswith(".tar.gz") and "nearby-cast" in name:
        print(asset["browser_download_url"])
        break
else:
    raise SystemExit(
        "No nearby-cast *.tar.gz on the latest GitHub Release. "
        "Create a release, or install via Flatpak (see README)."
    )
')

echo "Downloading..."
curl -fL "$ASSET_URL" -o "$TMP_DIR/nearby-cast.tar.gz"
tar -xzf "$TMP_DIR/nearby-cast.tar.gz" -C "$TMP_DIR"
PKG=$(find "$TMP_DIR" -maxdepth 2 -type d -name 'nearby-cast-*' | head -1)
if [ -z "$PKG" ]; then
  echo "Release archive layout unexpected." >&2
  exit 1
fi

echo "Installing to /usr/local (sudo)..."
# Stop the old broken installer hack that served stale UI on :1420
sudo pkill -f 'python3 -m http.server 1420 --directory /usr/share/nearby-cast/dist' 2>/dev/null || true

sudo mkdir -p /usr/share/nearby-cast/dist/assets
sudo mkdir -p /usr/local/bin /usr/share/applications
sudo mkdir -p /usr/share/icons/hicolor/scalable/apps
sudo rm -f /usr/share/nearby-cast/dist/assets/* 2>/dev/null || true

sudo cp "$PKG/cast_launcher.py" /usr/share/nearby-cast/cast_launcher.py
sudo cp "$PKG/dist/index.html" /usr/share/nearby-cast/dist/index.html
sudo cp "$PKG"/dist/assets/*.js /usr/share/nearby-cast/dist/assets/
sudo cp "$PKG/nearby-cast-bin" /usr/local/bin/nearby-cast-bin
sudo cp "$PKG/nearby-cast" /usr/local/bin/nearby-cast
sudo cp "$PKG/nearby-cast.desktop" /usr/share/applications/nearby-cast.desktop
sudo cp "$PKG/nearby-cast.svg" /usr/share/icons/hicolor/scalable/apps/nearby-cast.svg

sudo chmod +x /usr/share/nearby-cast/cast_launcher.py
sudo chmod +x /usr/local/bin/nearby-cast-bin /usr/local/bin/nearby-cast
sudo gtk-update-icon-cache -f -t /usr/share/icons/hicolor 2>/dev/null || true
sudo update-desktop-database 2>/dev/null || true

echo ""
echo "Installed."
echo "Run: nearby-cast"
echo ""
echo "Note: Google Cast needs python3 + pychromecast + ffmpeg + wf-recorder."
echo "Miracast extras (fluxcast/nmcli/…) are optional and not required for Cast."
