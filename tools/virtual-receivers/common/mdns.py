"""mDNS advertisement helpers using zeroconf."""
from __future__ import annotations

from typing import Optional

from zeroconf import ServiceInfo, Zeroconf

from . import local_ipv4


def make_zeroconf() -> Zeroconf:
    return Zeroconf()


def register_service(
    zc: Zeroconf,
    *,
    service_type: str,
    name: str,
    port: int,
    properties: dict[str, str],
    ip: Optional[str] = None,
) -> ServiceInfo:
    host_ip = ip or local_ipv4()
    instance = name.replace(" ", "-")
    # zeroconf >=0.132 expects a list of packed IPv4/IPv6 address bytes.
    packed = __import__("socket").inet_aton(host_ip)
    info = ServiceInfo(
        type_=service_type,
        name=f"{instance}.{service_type}",
        addresses=[packed],
        port=port,
        properties={key.encode(): value.encode() for key, value in properties.items()},
        server=f"{instance}.local.",
    )
    zc.register_service(info)
    return info
