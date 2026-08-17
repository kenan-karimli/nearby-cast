#!/usr/bin/env bash
set -euo pipefail

REPO="kenan-karimli/nearby-cast"
API="https://api.github.com/repos/${REPO}/releases/latest"
VERSION=""
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

command -v curl >/dev/null || { echo "curl is required." >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required." >&2; exit 1; }

ARCH=$(uname -m)
case "$ARCH" in
  x86_64|amd64) ;;
  *) echo "Unsupported architecture: $ARCH (only x86_64 is published)." >&2; exit 1 ;;
esac

RELEASE_JSON=$(curl -fsSL "$API")
VERSION=$(printf '%s' "$RELEASE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tag_name", "unknown"))')
mapfile -t ASSETS < <(printf '%s' "$RELEASE_JSON" | python3 -c '
import json, sys
release = json.load(sys.stdin)
if release.get("message"):
    raise SystemExit(release["message"])
for asset in release.get("assets", []):
    name = asset.get("name", "")
    if name.endswith((".deb", ".rpm", ".tar.gz")):
        print(asset["name"] + "\t" + asset["browser_download_url"])
')

if ((${#ASSETS[@]} == 0)); then
  echo "The latest GitHub Release has no supported x86_64 package." >&2
  exit 1
fi

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
fi
DISTRO_ID=${ID:-unknown}
DISTRO_LIKE=${ID_LIKE:-}
case "$DISTRO_ID" in
  arch|manjaro|endeavouros) DISTRO="Arch Linux"; FORMAT="tar.gz" ;;
  debian|ubuntu|linuxmint|pop) DISTRO="Debian/Ubuntu"; FORMAT="deb" ;;
  fedora|rhel|centos|rocky|almalinux) DISTRO="Fedora/RHEL"; FORMAT="rpm" ;;
  *)
    if [[ "$DISTRO_LIKE" == *debian* ]]; then
      DISTRO="Debian-family"; FORMAT="deb"
    elif [[ "$DISTRO_LIKE" == *fedora* || "$DISTRO_LIKE" == *rhel* ]]; then
      DISTRO="RPM-family"; FORMAT="rpm"
    else
      DISTRO="${PRETTY_NAME:-Unknown Linux}"; FORMAT="tar.gz"
    fi
    ;;
esac

echo "Detecting Linux distribution..."
echo "Detected: $DISTRO"
echo "Architecture: $ARCH"

choose_asset() {
  local line name url
  for line in "${ASSETS[@]}"; do
    name=${line%%$'\t'*}
    url=${line#*$'\t'}
    if [[ "$name" == *"x86_64"* || "$name" == *"amd64"* ]]; then
      if [[ "$name" == *".$FORMAT" ]]; then
        printf '%s\n%s\n' "$name" "$url"
        return
      fi
    fi
    if [[ "$FORMAT" == "tar.gz" && "$name" == *.tar.gz ]]; then
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

echo "Downloading NearbyCast ${VERSION}..."
curl --fail --location --silent --show-error "$ASSET_URL" -o "$TMP_DIR/$ASSET_NAME"

case "$ASSET_NAME" in
  *.deb)
    command -v apt-get >/dev/null || { echo "apt-get is required for .deb installation." >&2; exit 1; }
    if [[ "$(id -u)" -eq 0 ]]; then SUDO=""; elif command -v sudo >/dev/null; then SUDO="sudo"; else echo "sudo is required to install a .deb." >&2; exit 1; fi
    $SUDO apt-get install -y "$TMP_DIR/$ASSET_NAME"
    ;;
  *.rpm)
    if [[ "$(id -u)" -eq 0 ]]; then SUDO=""; elif command -v sudo >/dev/null; then SUDO="sudo"; else echo "sudo is required to install an .rpm." >&2; exit 1; fi
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
  *.tar.gz)
    INSTALL_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/nearby-cast"
    BIN_DIR="${HOME}/.local/bin"
    rm -rf "$TMP_DIR/extracted" "$INSTALL_ROOT"
    mkdir -p "$TMP_DIR/extracted" "$BIN_DIR"
    tar -xzf "$TMP_DIR/$ASSET_NAME" -C "$TMP_DIR/extracted"
    PACKAGE_DIR=$(find "$TMP_DIR/extracted" -mindepth 1 -maxdepth 1 -type d -print -quit)
    if [[ -z "$PACKAGE_DIR" || ! -x "$PACKAGE_DIR/nearby-cast" ]]; then
      echo "The portable release archive is missing its launcher." >&2
      exit 1
    fi
    mv "$PACKAGE_DIR" "$INSTALL_ROOT"
    ln -sfn "$INSTALL_ROOT/nearby-cast" "$BIN_DIR/nearby-cast"
    echo "NearbyCast installed successfully. Run: $BIN_DIR/nearby-cast"
    exit 0
    ;;
  *)
    echo "Unsupported release asset: $ASSET_NAME" >&2
    exit 1
    ;;
esac

echo "NearbyCast installed successfully. Run: nearby-cast"
