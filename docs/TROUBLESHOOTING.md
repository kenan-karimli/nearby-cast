# Troubleshooting

## Grey screen on Google Cast / Android TV

Symptom: Default Media Receiver opens, UI shows Connected, TV stays grey.

Cause: ultra-short HLS segments often leave Cast boxes in `BUFFERING`.

Fix in current builds: default transport is fragmented MP4 (`NEARBY_CAST_TRANSPORT=fmp4`).
NearbyCast waits for `PLAYING` before reporting casting. To force HLS:

```bash
export NEARBY_CAST_TRANSPORT=hls
```

## Capture permission / blank capture

* Grant ScreenCast portal permission when prompted.
* On Hyprland/wlroots, ensure `wf-recorder` and `xdg-desktop-portal-hyprland` work.
* Check the in-app dependency panel for `wf-recorder` / `ffmpeg`.

## Miracast peers missing

* Wi-Fi Direct requires NetworkManager P2P (`p2p-dev-*`) and FluxCast.
* `fluxcast --wfd-scan` with no peers means no physical Miracast sink is visible.
* LAN lab sinks use `_wfd-lab._tcp`, not Wi-Fi Direct.

## AirPlay Apple TV fails

FairPlay authentication is not implemented. Use an AirPlay lab receiver or
another protocol. Physical Apple TV remains `NOT VERIFIED`.

## Duplicate devices

Multiple advertisements for one endpoint are merged by stable identity / IP.
If duplicates remain, collect mDNS service names from diagnostics.

## Orphan ffmpeg processes

Stop casting from the UI (Stop). Sessions own children under a 0700 temp
directory. If the app is killed hard, leftover processes may remain — terminate
them and remove `/tmp/nearby-cast-*` session dirs owned by your user.
