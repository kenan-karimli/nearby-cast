#!/usr/bin/env bash
set -e

REPO="kenan-karimli/nearby-cast"
RAW_BASE="https://raw.githubusercontent.com/${REPO}/main/dist_pkg"
TMP_DIR=$(mktemp -d)

cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

echo "Nearby Cast - Installation"
echo "--------------------------"
echo ""

mkdir -p "${TMP_DIR}/dist/assets"

echo "Downloading..."
curl -fsSL "${RAW_BASE}/nearby-cast-bin"               -o "${TMP_DIR}/nearby-cast-bin"
curl -fsSL "${RAW_BASE}/nearby-cast"                   -o "${TMP_DIR}/nearby-cast"
curl -fsSL "${RAW_BASE}/cast_launcher.py"              -o "${TMP_DIR}/cast_launcher.py"
curl -fsSL "${RAW_BASE}/nearby-cast.desktop"           -o "${TMP_DIR}/nearby-cast.desktop"
curl -fsSL "${RAW_BASE}/nearby-cast.svg"               -o "${TMP_DIR}/nearby-cast.svg"
curl -fsSL "${RAW_BASE}/dist/index.html"               -o "${TMP_DIR}/dist/index.html"

# Extract actual bundle JS filename from index.html
ASSET_JS=$(grep -oP 'assets/\Kindex-[^"]+\.js' "${TMP_DIR}/dist/index.html" | head -1 || echo "")
if [ -z "$ASSET_JS" ]; then
    ASSET_JS="index-B69Zs4o8.js"
fi

curl -fsSL "${RAW_BASE}/dist/assets/${ASSET_JS}" -o "${TMP_DIR}/dist/assets/${ASSET_JS}"

echo "Installing..."
sudo mkdir -p /usr/share/nearby-cast/dist/assets
sudo mkdir -p /usr/local/bin
sudo mkdir -p /usr/share/applications
sudo mkdir -p /usr/share/icons/hicolor/scalable/apps

# Clean old bundle assets to avoid duplicates
sudo rm -f /usr/share/nearby-cast/dist/assets/* 2>/dev/null || true

sudo cp "${TMP_DIR}/cast_launcher.py"                           /usr/share/nearby-cast/cast_launcher.py
sudo cp "${TMP_DIR}/dist/index.html"                            /usr/share/nearby-cast/dist/index.html
sudo cp "${TMP_DIR}/dist/assets/${ASSET_JS}"             /usr/share/nearby-cast/dist/assets/${ASSET_JS}
sudo cp "${TMP_DIR}/nearby-cast-bin"                            /usr/local/bin/nearby-cast-bin
sudo cp "${TMP_DIR}/nearby-cast"                                /usr/local/bin/nearby-cast
sudo cp "${TMP_DIR}/nearby-cast.desktop"                        /usr/share/applications/nearby-cast.desktop
sudo cp "${TMP_DIR}/nearby-cast.svg"                            /usr/share/icons/hicolor/scalable/apps/nearby-cast.svg

sudo chmod +x /usr/share/nearby-cast/cast_launcher.py
sudo chmod +x /usr/local/bin/nearby-cast-bin
sudo chmod +x /usr/local/bin/nearby-cast

sudo gtk-update-icon-cache -f -t /usr/share/icons/hicolor 2>/dev/null || true
sudo update-desktop-database 2>/dev/null || true

echo ""
echo "Installed."
echo "Run: nearby-cast"
