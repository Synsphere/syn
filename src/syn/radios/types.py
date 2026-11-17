"""Immutable, evidence-bearing descriptions of locally installed radio interfaces."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
import hashlib
import json


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                      ensure_ascii=True).encode("ascii")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def clean(value: str, limit: int = 256) -> str:
    if not isinstance(value, str):
        raise TypeError("Radio descriptor labels must be text")
    return "".join(c for c in value if c.isprintable() and c not in "\r\n").strip()[:limit]


@dataclass(frozen=True, slots=True)
class RadioDevice:
    provider: str
    path: str
    category: str
    name: str
    transport: str
    driver: str | None = None
    chipset: str | None = None
    canReceive: bool | None = None
    canTransmit: bool | None = None
    features: tuple[str, ...] = ()
    bandsMHz: tuple[tuple[float, float], ...] = ()
    evidence: tuple[str, ...] = ()
    support: str = "identified"  # identified | driver | protocol
    details: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for key in ("provider", "path", "category", "name", "transport"):
            value = clean(getattr(self, key))
            if not value or len(value) > 256:
                raise ValueError(f"Invalid radio {key}")
            object.__setattr__(self, key, value)
        if self.support not in {"identified", "driver", "protocol"}:
            raise ValueError("Unknown radio support level")
        for key in ("canReceive", "canTransmit"):
            if getattr(self, key) is not None and type(getattr(self, key)) is not bool:
                raise TypeError("Radio capability must be true, false or unknown")
        if len(self.features) > 96 or len(self.evidence) > 32 or len(self.bandsMHz) > 32:
            raise ValueError("Oversized radio descriptor")
        object.__setattr__(self, "features", tuple(sorted(set(clean(x, 80) for x in self.features))))
        object.__setattr__(self, "evidence", tuple(sorted(set(clean(x, 256) for x in self.evidence))))
        object.__setattr__(self, "details", {clean(k, 80): clean(v) for k, v in self.details.items()})
        if len(self.details) > 32:
            raise ValueError("Oversized radio metadata")
        for lo, hi in self.bandsMHz:
            if not 0 < lo <= hi < 1e7:
                raise ValueError("Invalid frequency range")
        object.__setattr__(self, "bandsMHz", tuple(sorted(set(tuple(pair) for pair in self.bandsMHz))))

    @property
    def id(self) -> str:
        return digest([self.provider, self.transport, self.path])

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "deviceId": self.id}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RadioDevice:
        data = {k: v for k, v in raw.items() if k != "deviceId"}
        for key in ("features", "evidence", "bandsMHz"):
            if key in data:
                data[key] = tuple(tuple(x) if key == "bandsMHz" else x for x in data[key])
        return cls(**data)


@dataclass(frozen=True, slots=True)
class ProviderResult:
    name: str
    status: str  # complete or error; error cannot prove unplug
    devices: tuple[RadioDevice, ...] = ()
    errorType: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"complete", "error"}:
            raise ValueError("Invalid probe status")
        if self.status == "error" and self.devices:
            raise ValueError("Failed providers cannot claim a complete inventory")

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.name, "status": self.status, "count": len(self.devices),
                "errorType": self.errorType}
