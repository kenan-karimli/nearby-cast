# Protocols

NearbyCast selects transports automatically. Users are not asked to pick codecs
or RTSP/HLS details in the normal flow.

## Ranking (eligible only)

Lower practical latency is preferred among protocols that are both advertised
by the receiver and locally implementable:

1. Miracast (WFD RTSP/RTP) — when FluxCast + Wi-Fi Direct peer is available, or
   when a WFD lab sink is present
2. NearbyCast authenticated MPEG-TS — when a NearbyCast receiver is present
3. AirPlay lab mirroring — PIN + RTSP; physical FairPlay Apple TV is blocked
4. Google Cast — fragmented MP4 over HTTP/1.0 (default); HLS via
   `NEARBY_CAST_TRANSPORT=hls`

Advertisement alone never enables casting.

## Google Cast

Default transport is **live fragmented MP4** served as an HTTP/1.0 growing file
without `Content-Length`. On a physical Android TV Cast box (CNTV Plus Smart
Box / `Android TV` at the test LAN), this path reached sustained `PLAYING`.

The previous 0.2s HLS path left the same receiver stuck in `BUFFERING` (grey
screen). NearbyCast only reports casting after:

1. the receiver requests the media URL, and
2. player state is `PLAYING` for consecutive health samples

## Miracast

* Physical P2P: FluxCast + NetworkManager Wi-Fi Direct
* Lab/LAN IPv4: `tools/virtual-receivers/miracast/lab_sender.py` (RTSP/RTP)

## AirPlay

Lab sender/receiver implement PIN pairing + RTSP mirroring. FairPlay / Apple TV
authentication is not implemented and is refused honestly.

## NearbyCast

Ed25519 identity, pairing codes, persistent trust store, token-gated MPEG-TS
media. Unauthenticated media sockets are rejected.
