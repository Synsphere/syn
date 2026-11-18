"""Bounded scheduling for Syn's native Bluetooth advertising and observation."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class BluetoothConfig:
    # The example enables this; constructing an API without a configured node
    # must still be side-effect free.
    enabled: bool = False
    formats: tuple[str, ...] = ("syn", "syn-ibeacon", "ibeacon", "eddystone-uid")
    companyId: int | None = None
    frameDurationSeconds: float = .5
    listenSeconds: float = 20.0
    startupJitterSeconds: float = 5.0
    cycleJitterSeconds: float = 5.0
    inventoryRefreshSeconds: float = 10.0
    retrySeconds: float = 5.0
    backendTimeoutSeconds: float = 10.0
    observationWindowSeconds: float = 1.0
    maxPendingObservations: int = 512
    maxAdapters: int = 16

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("bluetooth.enabled must be true or false")
        allowed = {"syn", "syn-ibeacon", "ibeacon", "eddystone-uid", "altbeacon"}
        if (type(self.formats) is not tuple or not self.formats or len(self.formats) > len(allowed)
                or any(type(value) is not str or value not in allowed for value in self.formats)
                or len(set(self.formats)) != len(self.formats)):
            raise ValueError("bluetooth.formats must contain unique, supported Syn beacon formats")
        if not {"syn", "syn-ibeacon"}.intersection(self.formats):
            raise ValueError("bluetooth.formats requires a complete Syn identity transport: syn or syn-ibeacon")
        if self.companyId is not None and (type(self.companyId) is not int
                                           or not 0 <= self.companyId < 65535):
            raise ValueError("bluetooth.companyId must be an assigned 16-bit company identifier")
        if "altbeacon" in self.formats and self.companyId is None:
            raise ValueError("AltBeacon requires bluetooth.companyId; Syn has no assumed company identifier")
        for name, low, high in (
            ("frameDurationSeconds", .1, 10), ("listenSeconds", .1, 3600),
            ("startupJitterSeconds", 0, 60), ("cycleJitterSeconds", 0, 300),
            ("inventoryRefreshSeconds", .1, 300), ("retrySeconds", .1, 300),
            ("backendTimeoutSeconds", .1, 60), ("observationWindowSeconds", 0, 10),
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"bluetooth.{name} must be finite and between {low} and {high}")
        for name, upper in (("maxPendingObservations", 8192), ("maxAdapters", 64)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError(f"bluetooth.{name} must be an integer between 1 and {upper}")

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError("bluetooth must be a table")
        if set(value) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown bluetooth configuration field")
        values = dict(value)
        if "formats" in values:
            if type(values["formats"]) is not list:
                raise ValueError("bluetooth.formats must be a string array")
            values["formats"] = tuple(values["formats"])
        return cls(**values)
