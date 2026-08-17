"""Latency measurement helpers for same-host virtual lab sessions.

Timestamps use wall clock on a single machine. Values are never fabricated:
callers must record real send/receive events. When fewer than ``min_samples``
samples exist, percentile helpers return None.
"""
from __future__ import annotations

import json
import statistics
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


MAGIC = b"NCLAT1\0\0"  # 8 bytes


def now_ns() -> int:
    return time.time_ns()


def embed_timestamp(send_ns: int | None = None) -> bytes:
    """16-byte lab marker: magic + uint64 big-endian nanoseconds."""
    stamp = send_ns if send_ns is not None else now_ns()
    return MAGIC + struct.pack(">Q", stamp)


def extract_timestamps(chunk: bytes) -> list[int]:
    found: list[int] = []
    start = 0
    while True:
        index = chunk.find(MAGIC, start)
        if index < 0 or index + 16 > len(chunk):
            break
        (stamp,) = struct.unpack(">Q", chunk[index + 8 : index + 16])
        found.append(stamp)
        start = index + 16
    return found


@dataclass
class LatencySample:
    send_ns: int
    recv_ns: int

    @property
    def latency_ms(self) -> float:
        return max(0.0, (self.recv_ns - self.send_ns) / 1_000_000.0)


@dataclass
class LatencyReport:
    samples: list[LatencySample] = field(default_factory=list)
    startup_ms: Optional[float] = None
    frame_drops: int = 0
    bytes_received: int = 0
    duration_s: float = 0.0
    fps: Optional[float] = None
    bitrate_kbps: Optional[float] = None

    def add(self, send_ns: int, recv_ns: int | None = None) -> None:
        self.samples.append(LatencySample(send_ns=send_ns, recv_ns=recv_ns or now_ns()))

    def ingest_chunk(self, chunk: bytes, recv_ns: int | None = None) -> int:
        recv = recv_ns or now_ns()
        stamps = extract_timestamps(chunk)
        for stamp in stamps:
            self.add(stamp, recv)
        self.bytes_received += len(chunk)
        return len(stamps)

    def latencies_ms(self) -> list[float]:
        return [sample.latency_ms for sample in self.samples]

    def percentile(self, pct: float, *, min_samples: int = 5) -> Optional[float]:
        values = sorted(self.latencies_ms())
        if len(values) < min_samples:
            return None
        if pct <= 0:
            return values[0]
        if pct >= 100:
            return values[-1]
        rank = (len(values) - 1) * (pct / 100.0)
        low = int(rank)
        high = min(low + 1, len(values) - 1)
        weight = rank - low
        return values[low] * (1 - weight) + values[high] * weight

    def summary(self) -> dict:
        values = self.latencies_ms()
        return {
            "sample_count": len(values),
            "startup_ms": self.startup_ms,
            "average_ms": statistics.fmean(values) if values else None,
            "p50_ms": self.percentile(50),
            "p95_ms": self.percentile(95),
            "p99_ms": self.percentile(99),
            "min_ms": min(values) if values else None,
            "max_ms": max(values) if values else None,
            "frame_drops": self.frame_drops,
            "bytes_received": self.bytes_received,
            "duration_s": self.duration_s,
            "fps": self.fps,
            "bitrate_kbps": self.bitrate_kbps,
            "note": "same-host wall-clock; physical path NOT VERIFIED",
        }

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.summary(), indent=2), encoding="utf-8")
