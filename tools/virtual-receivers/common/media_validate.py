"""Inspect received media bytes for codec/container evidence."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class MediaEvidence:
    codec: Optional[str] = None
    container: Optional[str] = None
    has_pat_pmt: bool = False
    ts_packets: int = 0
    h264_nal_units: int = 0
    resolution_hint: Optional[str] = None


def inspect_mpegts(chunk: bytes) -> MediaEvidence:
    evidence = MediaEvidence(container="mpegts" if chunk[:1] == b"\x47" or b"\x47" in chunk[:188] else None)
    if len(chunk) < 188:
        return evidence
    offset = 0
    # Align to sync byte
    while offset < len(chunk) and chunk[offset] != 0x47:
        offset += 1
    packets = 0
    h264 = 0
    while offset + 188 <= len(chunk):
        if chunk[offset] != 0x47:
            offset += 1
            continue
        packet = chunk[offset : offset + 188]
        packets += 1
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        if pid in (0, 0x100, 0x101, 0x1100):
            evidence.has_pat_pmt = True
        # Look for Annex-B start codes inside payload
        payload = packet[4:]
        if b"\x00\x00\x00\x01" in payload or b"\x00\x00\x01" in payload:
            h264 += 1
            evidence.codec = evidence.codec or "h264"
        offset += 188
    evidence.ts_packets = packets
    evidence.h264_nal_units = h264
    return evidence


def inspect_rtp_mpegts(packet: bytes) -> MediaEvidence:
    if len(packet) < 12:
        return MediaEvidence()
    # RTP header is 12 bytes minimum; payload often MPEG-TS
    payload = packet[12:]
    evidence = inspect_mpegts(payload)
    if evidence.container is None and payload:
        evidence.container = "rtp"
    return evidence


def inspect_hls_playlist(text: str) -> MediaEvidence:
    evidence = MediaEvidence(container="hls")
    if "#EXTM3U" not in text:
        return evidence
    if ".ts" in text or ".m4s" in text:
        evidence.codec = evidence.codec or "h264"
    return evidence


def inspect_fmp4(chunk: bytes) -> MediaEvidence:
    """Return evidence when bytes look like a fragmented MP4 (ftyp/moof/mdat)."""
    evidence = MediaEvidence()
    if len(chunk) < 8:
        return evidence
    if b"ftyp" in chunk[:64] or b"moof" in chunk or b"mdat" in chunk:
        evidence.container = "fmp4"
        evidence.codec = "h264"
    return evidence
