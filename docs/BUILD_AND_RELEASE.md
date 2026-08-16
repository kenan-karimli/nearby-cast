# Build and release

Commands below were exercised in this checkout unless marked otherwise.

## Development validation

```bash
npm ci
npm run typecheck
npm test
npm run build
python3 -m py_compile cast_launcher.py
cargo fmt --check --manifest-path src-tauri/Cargo.toml
cargo clippy --manifest-path src-tauri/Cargo.toml -- -D warnings
cargo check --manifest-path src-tauri/Cargo.toml
cargo test --manifest-path src-tauri/Cargo.toml
npm run test:protocols
npm run test:failure
```

Virtual lab:

```bash
npm run virtual-receivers
# dashboard: http://127.0.0.1:8765/ui
```

Dev app with virtual receivers:

```bash
NEARBY_CAST_VIRTUAL_LAB=1 NEARBY_CAST_ALLOW_LOOPBACK=1 npm run tauri dev
```

Google Cast needs `python3`, `pychromecast`, `wf-recorder`, and `ffmpeg`.
Default transport is fMP4 (`NEARBY_CAST_TRANSPORT=fmp4`). Lab/CI may set
`NEARBY_CAST_LAB_MEDIA=1` and `NEARBY_CAST_LAB_CAST_LOAD=http://<ip>:<port>/load`.

## Production bundle (native)

```bash
npm ci
npm run build
cargo build --release --manifest-path src-tauri/Cargo.toml
# optional full Tauri bundler:
npm run tauri build
```

Verified native executable:

```text
src-tauri/target/release/nearby-cast
```

Optional bundler outputs (present in this tree; clean OS install not verified):

```text
src-tauri/target/release/bundle/deb/Nearby Cast_0.1.0_amd64.deb
src-tauri/target/release/bundle/rpm/Nearby Cast-0.1.0-1.x86_64.rpm
```

## Flatpak (primary Linux distribution target)

Runtime: **org.gnome.Platform // 49** (with `org.freedesktop.Platform.codecs-extra` **25.08-extra** for libx264; `wf-recorder` **v0.6.0**).

Rebuild from source (repository root):

```bash
flatpak-builder --user --force-clean --repo=repo-flatpak \
  build-flatpak flatpak/io.nearbycast.NearbyCast.yml
```

Install and run from the local repo:

```bash
flatpak --user remote-add --no-gpg-verify --if-not-exists nearbycast-local repo-flatpak
flatpak --user install -y nearbycast-local io.nearbycast.NearbyCast
flatpak run io.nearbycast.NearbyCast
```

One-shot build+install alternative:

```bash
flatpak-builder --user --install --force-clean build-flatpak flatpak/io.nearbycast.NearbyCast.yml
flatpak run io.nearbycast.NearbyCast
```

Manifest uses a local `dir` source and a network-using pip module for local builds.
For Flathub, replace the pip module with `flatpak-pip-generator` output and the
`dir` source with a tagged archive + sha256.

Validate metadata:

```bash
appstreamcli validate flatpak/io.nearbycast.NearbyCast.metainfo.xml
desktop-file-validate flatpak/io.nearbycast.NearbyCast.desktop
flatpak info --show-permissions io.nearbycast.NearbyCast
```

## Release gate

Do not publish until `docs/CURRENT_STATE.md` hardware rows match real evidence
and packaging has been install-tested. Flatpak install + launch was verified in
this workspace; deb/rpm clean-install remains PARTIAL.
