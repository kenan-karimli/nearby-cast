#!/usr/bin/env bash
set -euo pipefail

REPO="kenan-karimli/nearby-cast"
API="https://api.github.com/repos/${REPO}/releases/latest"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

command -v curl >/dev/null || { echo "curl is required." >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required." >&2; exit 1; }

ARCH=$(uname -m)
case "$ARCH" in
  x86_64|amd64) ;;
  *) echo "Unsupported architecture: $ARCH (only x86_64 is published)." >&2; exit 1 ;;
esac

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=""
elif command -v sudo >/dev/null; then
  SUDO="sudo"
else
  echo "sudo is required when installing as a regular user." >&2
  exit 1
fi

mapfile -t ASSETS < <(curl -fsSL "$API" | python3 -c '
import json, sys
release = json.load(sys.stdin)
if release.get("message"):
    raise SystemExit(release["message"])
for asset in release.get("assets", []):
    name = asset.get("name", "")
    if name.endswith((".deb", ".rpm")):
        print(asset["name"] + "\t" + asset["browser_download_url"])
')

if ((${#ASSETS[@]} == 0)); then
  echo "The latest GitHub Release has no x86_64 .deb or .rpm asset." >&2
  exit 1
fi

choose_asset() {
  local line name url
  for line in "${ASSETS[@]}"; do
    name=${line%%$'\t'*}
    url=${line#*$'\t'}
    if [[ -f /etc/debian_version && "$name" == *.deb ]]; then
      printf '%s\n%s\n' "$name" "$url"
      return
    fi
    if [[ -f /etc/redhat-release || -f /etc/fedora-release ]] && [[ "$name" == *.rpm ]]; then
      printf '%s\n%s\n' "$name" "$url"
      return
    fi
  done
  for line in "${ASSETS[@]}"; do
    name=${line%%$'\t'*}
    url=${line#*$'\t'}
    if [[ "$name" == *.deb || "$name" == *.rpm ]]; then
      printf '%s\n%s\n' "$name" "$url"
      return
    fi
  done
}

mapfile -t CHOSEN < <(choose_asset)
ASSET_NAME=${CHOSEN[0]:-}
ASSET_URL=${CHOSEN[1]:-}
if [[ -z "$ASSET_NAME" || -z "$ASSET_URL" ]]; then
  echo "Could not select a compatible release asset." >&2
  exit 1
fi

echo "Downloading NearbyCast ${ASSET_NAME}..."
curl --fail --location --silent --show-error "$ASSET_URL" -o "$TMP_DIR/$ASSET_NAME"

case "$ASSET_NAME" in
  *.deb)
    command -v apt-get >/dev/null || { echo "apt-get is required for .deb installation." >&2; exit 1; }
    $SUDO apt-get install -y "$TMP_DIR/$ASSET_NAME"
    ;;
  *.rpm)
    if command -v dnf >/dev/null; then
      $SUDO dnf install -y "$TMP_DIR/$ASSET_NAME"
    elif command -v zypper >/dev/null; then
      $SUDO zypper --non-interactive install "$TMP_DIR/$ASSET_NAME"
    elif command -v rpm >/dev/null; then
      $SUDO rpm -U "$TMP_DIR/$ASSET_NAME"
    else
      echo "No supported RPM installer found (dnf, zypper, or rpm)." >&2
      exit 1
    fi
    ;;
  *)
    echo "Unsupported release asset: $ASSET_NAME" >&2
    exit 1
    ;;
esac

echo "NearbyCast installed successfully. Run: nearby-cast"
