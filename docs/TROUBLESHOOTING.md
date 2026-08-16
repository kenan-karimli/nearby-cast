# Troubleshooting

## Grey screen / TV never shows the desktop (Google Cast)

Symptom: App says Casting, TV stays grey or blank; bitrate may be N/A.

Cause: some Cast boxes never leave `BUFFERING` on HLS.

Current default is **fMP4** (compatible). NearbyCast only marks casting after
`PLAYING` + a media GET. If you want lower delay and your TV accepts HLS:

```bash
NEARBY_CAST_TRANSPORT=hls nearby-cast
```

## High delay (~10–20s) on Google Cast

Growing-file **fMP4** makes Default Media Receiver buffer heavily. That is a
Cast receiver limit, not dropped frames. Try HLS for ~3–5s when it works.
Miracast/NearbyCast are the low-latency paths when available.

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
