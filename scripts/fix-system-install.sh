#!/usr/bin/env bash
# One-shot fix for the broken system install that showed:
#   sudo pacman -S fluxcast mpv nmcli slurp wlr-randr
# Run from the NearbyCast checkout (needs your sudo password once).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ ! -x dist_pkg/nearby-cast-bin ]; then
  echo "Building release binary…"
  npm run build
  cargo build --release --manifest-path src-tauri/Cargo.toml
  cp -f src-tauri/target/release/nearby-cast dist_pkg/nearby-cast-bin
  cp -f cast_launcher.py dist_pkg/cast_launcher.py
  rm -rf dist_pkg/dist && mkdir -p dist_pkg/dist/assets
  cp -f dist/index.html dist_pkg/dist/
  cp -f dist/assets/*.js dist_pkg/dist/assets/
  cat > dist_pkg/nearby-cast <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
SHARE_DIR=/usr/share/nearby-cast
export CAST_LAUNCHER="${CAST_LAUNCHER:-$SHARE_DIR/cast_launcher.py}"
exec /usr/local/bin/nearby-cast-bin "$@"
EOF
  chmod +x dist_pkg/nearby-cast dist_pkg/nearby-cast-bin
fi

echo "Stopping stale :1420 UI server (if any)…"
# Prefer port-based kill so we do not match this script text.
python3 - <<'PY'
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
                print(f"killed pid {pid}")
    except Exception:
        pass
PY

echo "Installing fixed build to /usr/local (sudo)…"
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

echo ""
echo "Fixed. Fully quit Nearby Cast, then run:"
echo "  nearby-cast"
echo "The red pacman/fluxcast warning must be gone for Google Cast."
