"""Bounded clock policy. Resolution is never substituted for UTC accuracy."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import math
import re


def number(value, name, minimum=0, maximum=1e12):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not minimum <= value <= maximum):
        raise ValueError(f"{name} must be finite and between {minimum} and {maximum}")
    return float(value)


@dataclass(frozen=True, slots=True)
class ClockConfig:
    ntpServers: tuple[str, ...] = ("0.pool.ntp.org", "1.pool.ntp.org", "2.pool.ntp.org")
    ntpSeconds: float = 64.0
    pollSeconds: float = 10.0
    timeoutSeconds: float = 3.0
    maxAgeSeconds: float = 300.0
    holdoverPpm: float = 50.0
    stepThresholdMs: float = 100.0
    windowSize: int = 32
    maxSources: int = 128
    chrony: bool = True
    hardware: bool = True
    gpsTransportErrorMs: float | None = 1000.0
    gpsdHosts: tuple[str, ...] = ("127.0.0.1:2947",)
    # Device policies are explicit accuracy assertions, not inferred from resolution.
    # path, timescale (UTC/TAI/unknown), maxErrorMs, taiUtcOffsetSeconds, driftPpm,
    # calibratedAt (Unix seconds for RTC), pmcSocket (optional PHC health check).
    ptpDevices: tuple[dict, ...] = ()
    rtcDevices: tuple[dict, ...] = ()

    def __post_init__(self):
        for name in ("ntpSeconds", "pollSeconds"):
            number(getattr(self, name), name, 1, 86400)
        number(self.timeoutSeconds, "timeoutSeconds", .05, 30)
        number(self.maxAgeSeconds, "maxAgeSeconds", 1, 86400)
        number(self.holdoverPpm, "holdoverPpm", .001, 1e6)
        number(self.stepThresholdMs, "stepThresholdMs", .001, 60000)
        for name, lo, hi in (("windowSize", 3, 256), ("maxSources", 1, 1024)):
            if type(getattr(self, name)) is not int or not lo <= getattr(self, name) <= hi:
                raise ValueError(f"Invalid {name}")
        for name in ("chrony", "hardware"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        if self.gpsTransportErrorMs is not None:
            number(self.gpsTransportErrorMs, "gpsTransportErrorMs", 0, 60000)
        if not self.ntpServers or len(self.ntpServers) > 16:
            raise ValueError("At least one NTP server is required (maximum 16)")
        for host in self.ntpServers:
            if not isinstance(host, str) or len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9.:%_-]+", host):
                raise ValueError("NTP server must be a hostname or IP literal, without credentials")
        from syn.positions.providers.gpsd import endpoint
        if len(self.gpsdHosts) > 16:
            raise ValueError("Too many gpsd endpoints")
        for host in self.gpsdHosts:
            endpoint(host)
        allowed = {"path", "timescale", "maxErrorMs", "taiUtcOffsetSeconds", "driftPpm", "calibratedAt", "pmcSocket"}
        for collection, prefix in ((self.ptpDevices, "ptp"), (self.rtcDevices, "rtc")):
            if len(collection) > 32:
                raise ValueError("Too many hardware clock devices")
            seen = set()
            for item in collection:
                if not isinstance(item, dict) or set(item) - allowed:
                    raise ValueError("Unknown hardware clock policy field")
                path = item.get("path", "")
                if not isinstance(path, str) or not re.fullmatch(r"/dev/"+prefix+r"[0-9]+", path) or path in seen:
                    raise ValueError("Device policy needs a unique /dev/ptpN or /dev/rtcN path")
                seen.add(path)
                scale = item.get("timescale", "unknown")
                if scale not in {"UTC", "TAI", "unknown"} or (prefix == "rtc" and scale == "TAI"):
                    raise ValueError("Unsupported hardware clock timescale")
                for name in ("maxErrorMs", "driftPpm", "calibratedAt"):
                    if item.get(name) is not None:
                        number(item[name], name)
                if item.get("taiUtcOffsetSeconds") is not None:
                    if type(item["taiUtcOffsetSeconds"]) is not int or not 0 <= item["taiUtcOffsetSeconds"] <= 1000:
                        raise ValueError("Invalid TAI minus UTC offset")
                if item.get("pmcSocket") is not None:
                    if prefix != "ptp" or not isinstance(item["pmcSocket"], str) or not item["pmcSocket"].startswith("/"):
                        raise ValueError("pmcSocket must be an absolute local Unix socket")

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise ValueError("Unknown clock configuration field or non-table clock configuration")
        result = dict(value)
        for name in ("ntpServers", "gpsdHosts", "ptpDevices", "rtcDevices"):
            if name in result:
                if not isinstance(result[name], (tuple, list)):
                    raise ValueError(f"{name} must be an array")
                result[name] = tuple(result[name])
        return cls(**result)

    def to_dict(self):
        return asdict(self)
