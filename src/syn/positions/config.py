"""Validated, process-serializable node-local positioning settings."""
from dataclasses import asdict, dataclass, fields
import math
from .types import local_ref


@dataclass(frozen=True)
class PositionConfig:
    nodeRef: str | None = None
    stateDirectory: str = ".syn"
    timezone: str | None = None
    pollSeconds: float = 5.0
    rescanSeconds: float = 300.0
    probeSeconds: float = 2.0
    readSeconds: float = 3.0
    maxAgeSeconds: float = 120.0
    futureToleranceSeconds: float = 5.0
    driftMetersPerSecond: float = 2.0
    uereMeters: float = 5.0
    gpsdHosts: tuple[str, ...] = ("127.0.0.1:2947", "[::1]:2947")
    serialPorts: tuple[str, ...] = ()
    excludedSerialPorts: tuple[str, ...] = ()
    serialBaudRates: tuple[int, ...] = (4800, 9600, 38400, 115200)
    serialProbe: bool = True
    nativeServices: bool = True
    bluetoothLE: bool = True
    networkNmea: tuple[str, ...] = ()
    maxDevices: int = 64
    maxConcurrentJobs: int = 4
    timezoneCatalogue: str | None = None

    def __post_init__(self):
        if self.nodeRef is not None:
            local_ref(self.nodeRef, "thing")
        for name in ("pollSeconds", "rescanSeconds", "probeSeconds", "readSeconds", "maxAgeSeconds", "uereMeters"):
            n = getattr(self, name)
            if type(n) not in (int, float) or not math.isfinite(n) or not 0 < n <= 86400:
                raise ValueError(f"position.{name} must be finite and in (0,86400]")
        if self.pollSeconds < 1 or self.rescanSeconds < 10:
            raise ValueError("Polling minimum is 1 second; rediscovery minimum is 10 seconds")
        for name in ("futureToleranceSeconds", "driftMetersPerSecond"):
            n = getattr(self, name)
            if type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n <= 10000:
                raise ValueError(f"Invalid position.{name}")
        for name in ("maxDevices", "maxConcurrentJobs"):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= 128:
                raise ValueError(f"Invalid position.{name}")
        for name in ("serialProbe", "nativeServices", "bluetoothLE"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"position.{name} must be boolean")
        for name in ("gpsdHosts", "serialPorts", "excludedSerialPorts", "networkNmea"):
            value = getattr(self, name)
            if not isinstance(value, (tuple, list)) or len(value) > 128 or any(not isinstance(x, str) or not x or len(x) > 512 for x in value):
                raise ValueError(f"Invalid position.{name}")
            object.__setattr__(self, name, tuple(value))
        if not self.serialBaudRates or len(self.serialBaudRates) > 16 or any(type(n) is not int or not 300 <= n <= 4_000_000 for n in self.serialBaudRates):
            raise ValueError("Invalid serial baud rates")
        object.__setattr__(self, "serialBaudRates", tuple(self.serialBaudRates))
        if not isinstance(self.stateDirectory, str) or not self.stateDirectory:
            raise ValueError("position.stateDirectory is required")
        for name in ("timezone", "timezoneCatalogue"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"Invalid position.{name}")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise ValueError("Unknown position settings")
        return cls(**value)
