"""Bounded provider values. Descriptors contain no credentials or live handles."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Device:
    provider: str
    address: str
    model: str | None = None
    serialNumber: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    claimedPorts: tuple[str, ...] = ()

    @property
    def id(self):
        from ..types import digest
        return self.provider + "-" + digest({"address": self.address,
                                            "serialNumber": self.serialNumber})[:32]

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, body):
        return cls(**body)


@dataclass
class Probe:
    provider: str
    status: str
    devices: list[Device] = field(default_factory=list)
    detail: str | None = None

    def to_dict(self):
        return {"provider": self.provider, "status": self.status,
                "deviceIds": [d.id for d in self.devices], "detail": self.detail}


class ProviderUnavailable(RuntimeError):
    """Missing driver, permission, service, or disconnected device."""
