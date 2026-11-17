"""Radio discovery policy. Passive enumeration is always the default."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import math


@dataclass(frozen=True, slots=True)
class RadioConfig:
    enabled: bool = True
    rescanSeconds: float = 300.0
    probeTimeoutSeconds: float = 2.5
    maxDevices: int = 128
    useCommands: bool = True
    # Some Soapy drivers perform network discovery; require explicit opt-in.
    useSoapySDR: bool = False
    useSysfs: bool = True
    # The explicit ports must run a cooperative SYN-RADIOLIB/1 firmware bridge.
    # No automatic writing to unknown serial ports, SPI registers or I2C addresses.
    radioLibPorts: tuple[str, ...] = ()
    excludedProviders: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or type(self.useCommands) is not bool:
            raise ValueError("Invalid radio boolean")
        if type(self.useSysfs) is not bool or type(self.useSoapySDR) is not bool:
            raise ValueError("Invalid radio boolean")
        for value, low, high in ((self.rescanSeconds, 10, 86400),
                                  (self.probeTimeoutSeconds, .1, 20)):
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("Invalid radio interval")
        if type(self.maxDevices) is not int or not 1 <= self.maxDevices <= 512:
            raise ValueError("Invalid radio maximum")
        for key in ("radioLibPorts", "excludedProviders"):
            values = getattr(self, key)
            if not isinstance(values, (tuple, list)) or len(values) > 64 or any(
                not isinstance(v, str) or not v or len(v) > 256 for v in values
            ):
                raise ValueError(f"Invalid radio {key}")
            object.__setattr__(self, key, tuple(values))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: dict) -> RadioConfig:
        if not isinstance(values, dict) or set(values) - {x.name for x in fields(cls)}:
            raise ValueError("Unknown radio configuration fields")
        return cls(**values)
