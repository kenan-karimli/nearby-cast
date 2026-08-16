# NearbyCast implementation plan

This document tracks vertical slices that produced the current product state.
It is not a substitute for `docs/CURRENT_STATE.md` evidence.

## Completed slices (evidence-backed)

1. Shared foundation — discovery merge, capability verification fields, router
2. Virtual receiver lab — Cast / Miracast / AirPlay / NearbyCast + suites
3. Capture + encoder probes — wf-recorder path; lavfi lab; one-frame probes
4. NearbyCast auth media — pairing + trust + token MPEG-TS (simulator)
5. Google Cast — **fMP4 growing-file transport**; PLAYING gate; physical Android TV PASS
6. Miracast lab RTSP/RTP + FluxCast P2P boundary
7. AirPlay lab mirroring; FairPlay blocked honestly
8. Bounded reconnect + UI honesty for metrics/stages

## Remaining / blocked

* Physical Miracast P2P (no peers on current Wi-Fi)
* AirPlay FairPlay / Apple TV
* Flatpak install-tested Flathub submission (pip sources must be vendored)
* AppImage (linuxdeploy failure historically)
* Clean-install matrix on a second machine

## Do not

* Mark physical_verified without hardware proof
* Report Connected without PLAYING / media acceptance evidence
* Prefer HLS for Cast when fMP4 works on the receiver class
