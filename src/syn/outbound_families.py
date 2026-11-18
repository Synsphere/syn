"""Small, non-invasive outbound IP-family capability probe.

A bound IPv6 listener or an AAAA DNS record does not establish an IPv6 route.
The UDP connect here performs route/source selection without transmitting data.
Failure suppresses IPv6 *dial candidates*, never previously authenticated peers.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import socket
import time
from typing import Callable


_IPV6_ROUTE_PROBE = "2606:4700:4700::1111"


def outbound_ipv6_route() -> bool:
    """Return whether the kernel can select a globally routable IPv6 source."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as probe:
            # UDP connect does not transmit packets. It only selects the route.
            probe.connect((_IPV6_ROUTE_PROBE, 443, 0, 0))
            return ipaddress.IPv6Address(probe.getsockname()[0]).is_global
    except (OSError, ValueError):
        return False


def in_wsl() -> bool:
    if os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="ascii").lower()
    except OSError:
        return False
    return "microsoft" in release or "wsl" in release


class OutboundFamilies:
    """One cached, bounded route decision; automatically recovers after a TTL."""

    def __init__(self, probe: Callable[[], bool] = outbound_ipv6_route,
                 *, ttl_seconds: float = 60.0, clock: Callable[[], float] = time.monotonic):
        if not 1 <= ttl_seconds <= 3600:
            raise ValueError("Route probe TTL must be between 1 and 3600 seconds")
        self._probe = probe
        self._ttl = ttl_seconds
        self._clock = clock
        self._expires = 0.0
        self._ipv6 = False
        self._checked = False

    def ipv6(self) -> bool:
        now = self._clock()
        if not self._checked or now >= self._expires:
            try:
                self._ipv6 = bool(self._probe())
            except (OSError, ValueError):
                self._ipv6 = False
            self._checked = True
            self._expires = now + self._ttl
        return self._ipv6

    def status(self) -> dict:
        return {"outboundIpv6Route": self.ipv6(), "ipv6ProbeTtlSeconds": self._ttl}


# Shared between the DHT resolver and actual peerstore dials. The bounded TTL
# permits IPv6 routes to recover without restarting Syn.
OUTBOUND_FAMILIES = OutboundFamilies()
