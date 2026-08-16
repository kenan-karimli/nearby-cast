#!/usr/bin/env bash
# Install a real production Nearby Cast binary to /usr/local.
# Plain `cargo build --release` is NOT enough — that binary still loads
# http://localhost:1420 and shows "Connection refused" without a Vite server.
# This script uses `tauri build` so the UI is embedded.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "Building production binary (tauri build, no AppImage)…"
npm run build
# --bundles deb avoids the optional AppImage/linuxdeploy failure on some hosts
npm run tauri build -- --bundles deb

cp -f "$ROOT/src-tauri/target/release/nearby-cast" dist_pkg/nearby-cast-bin
cp -f cast_launcher.py dist_pkg/cast_launcher.py
rm -rf dist_pkg/dist && mkdir -p dist_pkg/dist/assets
cp -f dist/index.html dist_pkg/dist/
cp -f dist/assets/*.js dist_pkg/dist/assets/
cat > dist_pkg/nearby-cast <<'EOF'
#!/usr/bin/env bash
# Nearby Cast launcher — production binary embeds the UI.
set -euo pipefail
SHARE_DIR=/usr/share/nearby-cast
export CAST_LAUNCHER="${CAST_LAUNCHER:-$SHARE_DIR/cast_launcher.py}"
exec /usr/local/bin/nearby-cast-bin "$@"
EOF
chmod +x dist_pkg/nearby-cast dist_pkg/nearby-cast-bin

# Stop any leftover :1420 UI hack from older broken installs
python3 - <<'PY' || true
import os, re
want = 1420
inodes = set()
for path in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        lines = open(path).read().splitlines()[1:]
    except FileNotFoundError:
        continue
    for line in lines:
        parts = line.split()
        _ip, port = parts[1].split(":")
        if int(port, 16) == want and parts[3] == "0A":
            inodes.add(parts[9])
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    fd = f"/proc/{pid}/fd"
    try:
        for ent in os.listdir(fd):
            try:
                target = os.readlink(f"{fd}/{ent}")
            except Exception:
                continue
            m = re.match(r"socket:\[(\d+)\]", target)
            if m and m.group(1) in inodes:
                os.kill(int(pid), 9)
    except Exception:
        pass
PY

echo "Installing to /usr/local (sudo)…"
sudo mkdir -p /usr/share/nearby-cast/dist/assets /usr/local/bin \
  /usr/share/applications /usr/share/icons/hicolor/scalable/apps
sudo rm -f /usr/share/nearby-cast/dist/assets/*
sudo cp dist_pkg/cast_launcher.py /usr/share/nearby-cast/cast_launcher.py
sudo cp dist_pkg/dist/index.html /usr/share/nearby-cast/dist/index.html
sudo cp dist_pkg/dist/assets/*.js /usr/share/nearby-cast/dist/assets/
sudo cp dist_pkg/nearby-cast-bin /usr/local/bin/nearby-cast-bin
sudo cp dist_pkg/nearby-cast /usr/local/bin/nearby-cast
sudo cp dist_pkg/nearby-cast.desktop /usr/share/applications/nearby-cast.desktop
sudo cp dist_pkg/nearby-cast.svg /usr/share/icons/hicolor/scalable/apps/nearby-cast.svg
sudo chmod +x /usr/share/nearby-cast/cast_launcher.py \
  /usr/local/bin/nearby-cast-bin /usr/local/bin/nearby-cast
sudo gtk-update-icon-cache -f -t /usr/share/icons/hicolor 2>/dev/null || true
sudo update-desktop-database 2>/dev/null || true

echo ""
echo "Installed production build. Fully quit Nearby Cast, then run:"
echo "  nearby-cast"
echo "Window must open with the dark UI (no 'localhost / Connection refused')."
