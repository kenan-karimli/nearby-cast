"""Shared helpers for NearbyCast virtual receivers."""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Optional


def lab_enabled() -> bool:
    return os.environ.get("NEARBY_CAST_VIRTUAL_LAB", "").strip() in {"1", "true", "yes"}


def allow_loopback() -> bool:
    return os.environ.get("NEARBY_CAST_ALLOW_LOOPBACK", "").strip() in {"1", "true", "yes"} or lab_enabled()


def local_ipv4() -> str:
    preferred = os.environ.get("NEARBY_CAST_LAB_BIND_IP", "").strip()
    if preferred:
        return preferred
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Metrics:
    discovery_time_ms: Optional[float] = None
    connection_time_ms: Optional[float] = None
    authentication_time_ms: Optional[float] = None
    negotiation_time_ms: Optional[float] = None
    first_frame_time_ms: Optional[float] = None
    average_fps: Optional[float] = None
    dropped_packets: int = 0
    received_packets: int = 0
    received_bytes: int = 0
    bitrate_kbps: Optional[float] = None
    reconnect_time_ms: Optional[float] = None
    shutdown_time_ms: Optional[float] = None
    codec: Optional[str] = None
    resolution: Optional[str] = None
    has_audio: Optional[bool] = None
    last_error: Optional[str] = None
    latency_samples_ms: list = field(default_factory=list)
    latency_p50_ms: Optional[float] = None
    latency_p95_ms: Optional[float] = None
    latency_p99_ms: Optional[float] = None

    def record_latency_ms(self, latency_ms: float, *, max_samples: int = 5000) -> None:
        self.latency_samples_ms.append(float(latency_ms))
        if len(self.latency_samples_ms) > max_samples:
            self.latency_samples_ms = self.latency_samples_ms[-max_samples:]
        self._refresh_latency_percentiles()

    def _refresh_latency_percentiles(self) -> None:
        values = sorted(self.latency_samples_ms)
        if len(values) < 5:
            self.latency_p50_ms = None
            self.latency_p95_ms = None
            self.latency_p99_ms = None
            return

        def pct(p: float) -> float:
            rank = (len(values) - 1) * (p / 100.0)
            low = int(rank)
            high = min(low + 1, len(values) - 1)
            weight = rank - low
            return values[low] * (1 - weight) + values[high] * weight

        self.latency_p50_ms = pct(50)
        self.latency_p95_ms = pct(95)
        self.latency_p99_ms = pct(99)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # Keep summaries in snapshots; omit raw sample arrays from dashboard noise.
        payload.pop("latency_samples_ms", None)
        payload["latency_sample_count"] = len(self.latency_samples_ms)
        return payload


@dataclass
class DeviceState:
    protocol: str
    name: str
    ip: str
    port: int
    advertisement: str = "stopped"
    connection: str = "idle"
    authentication: str = "idle"
    stream: str = "idle"
    session_duration_s: float = 0.0
    metrics: Metrics = field(default_factory=Metrics)
    extras: dict[str, Any] = field(default_factory=dict)
    started_at: Optional[float] = None

    def snapshot(self) -> dict[str, Any]:
        duration = 0.0
        if self.started_at is not None:
            duration = max(0.0, time.time() - self.started_at)
        return {
            "protocol": self.protocol,
            "name": self.name,
            "ip": self.ip,
            "port": self.port,
            "advertisement": self.advertisement,
            "connection": self.connection,
            "authentication": self.authentication,
            "stream": self.stream,
            "session_duration_s": duration,
            "metrics": self.metrics.to_dict(),
            "extras": self.extras,
        }


class LabBus:
    """Process-local control plane for the virtual lab dashboard/API."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.devices: dict[str, DeviceState] = {}
        self.events: list[dict[str, Any]] = []
        self.inject: dict[str, Any] = {
            "latency_ms": 0,
            "packet_loss": 0.0,
            "reject_auth": False,
            "reject_media": False,
            "crash": False,
            "airplay_refuse_pair": False,
            "airplay_malformed_rtsp": False,
            "miracast_malformed_rtsp": False,
            "disappear": False,
            "close_after_setup": False,
        }

    def upsert(self, key: str, state: DeviceState) -> None:
        with self._lock:
            self.devices[key] = state

    def get(self, key: str) -> Optional[DeviceState]:
        with self._lock:
            return self.devices.get(key)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "devices": {key: device.snapshot() for key, device in self.devices.items()},
                "inject": dict(self.inject),
                "events": list(self.events[-200:]),
            }

    def emit(self, kind: str, **payload: Any) -> None:
        with self._lock:
            self.events.append({"ts": now_ms(), "kind": kind, **payload})

    def set_inject(self, **kwargs: Any) -> None:
        with self._lock:
            self.inject.update(kwargs)


BUS = LabBus()


def write_results(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def wait_until(predicate: Callable[[], bool], timeout_s: float, interval_s: float = 0.1) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return False
