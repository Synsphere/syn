"""Explicit trusted Cardano endpoints; no public relay discovery or implicit dial."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import math
import re


_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def peer_address(value: str) -> tuple[str, int]:
    """Parse a canonical hostname:port or [IPv6]:port chosen by the operator."""
    if type(value) is not str or not 1 <= len(value) <= 320 or value != value.strip():
        raise ValueError("Cardano peers must be bounded host:port addresses")
    if value.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\]:([0-9]+)", value)
        if match is None:
            raise ValueError("Expected [IPv6]:port for a Cardano peer")
        host, port_text = match.groups()
        try:
            address = ipaddress.IPv6Address(host)
        except ValueError:
            raise ValueError("Invalid Cardano peer IPv6 address") from None
        if str(address) != host or address.scope_id is not None:
            raise ValueError("Cardano peer IPv6 address must be canonical and unscoped")
    else:
        if value.count(":") != 1:
            raise ValueError("Cardano peers require an explicit TCP port")
        host, port_text = value.split(":")
        try:
            address = ipaddress.IPv4Address(host)
        except ValueError:
            address = None
            if (len(host) > 253 or not host or host.isdecimal()
                    or all(c in "0123456789." for c in host)
                    or any(_DNS_LABEL.fullmatch(label) is None for label in host.split("."))):
                raise ValueError("Invalid Cardano peer hostname") from None
        if address is not None and str(address) != host:
            raise ValueError("Cardano peer IPv4 address must be canonical")
    if (not port_text.isascii() or not port_text.isdecimal() or len(port_text) > 5
            or str(int(port_text)) != port_text or not 1 <= int(port_text) <= 65535):
        raise ValueError("Cardano peer port must be between 1 and 65535")
    if address is not None and (address.is_unspecified or address.is_multicast):
        raise ValueError("Cardano peer must be a unicast destination")
    return host, int(port_text)


@dataclass(frozen=True, slots=True)
class CardanoConfig:
    enabled: bool = False
    networkMagic: int = 764824073
    peers: tuple[str, ...] = ()
    startSlot: int | None = None
    startHash: str | None = None
    timeoutSeconds: float = 15.0
    keepaliveIntervalSeconds: float = 20.0
    reconnectDelaySeconds: float = 2.0
    maxReconnectDelaySeconds: float = 60.0

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("cardano.enabled must be true or false")
        if type(self.networkMagic) is not int or not 0 <= self.networkMagic <= 0xffffffff:
            raise ValueError("cardano.networkMagic must be an unsigned 32-bit integer")
        if (type(self.peers) is not tuple or len(self.peers) > 8
                or any(type(peer) is not str for peer in self.peers)
                or len(set(self.peers)) != len(self.peers)):
            raise ValueError("cardano.peers must contain at most eight unique trusted endpoints")
        for peer in self.peers:
            peer_address(peer)
        if self.enabled and not self.peers:
            raise ValueError("Enabled Cardano observation requires at least one trusted peer")
        if (self.startSlot is None) != (self.startHash is None):
            raise ValueError("cardano.startSlot and startHash must be supplied together")
        if self.startSlot is not None:
            if type(self.startSlot) is not int or not 0 <= self.startSlot <= 2**63 - 1:
                raise ValueError("cardano.startSlot must be a non-negative signed 64-bit integer")
            if type(self.startHash) is not str or _HASH.fullmatch(self.startHash) is None:
                raise ValueError("cardano.startHash must be a lowercase 32-byte block hash")
        for name, low, high in (("timeoutSeconds", .1, 300),
                                ("keepaliveIntervalSeconds", 1, 60),
                                ("reconnectDelaySeconds", .1, 60),
                                ("maxReconnectDelaySeconds", .1, 600)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"cardano.{name} must be finite and between {low} and {high}")
        if self.maxReconnectDelaySeconds < self.reconnectDelaySeconds:
            raise ValueError("cardano.maxReconnectDelaySeconds cannot be below reconnectDelaySeconds")

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError("cardano must be a table")
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError("Unknown cardano configuration field")
        values = dict(value)
        peers = values.get("peers", [])
        if type(peers) is not list:
            raise ValueError("cardano.peers must be a string array")
        values["peers"] = tuple(peers)
        return cls(**values)
