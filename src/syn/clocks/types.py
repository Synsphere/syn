"""A clock observation pairs reference UTC with the *uncorrected* system clock."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
import time

from .config import number

KINDS = {"ntp", "gps", "gpsd-pps", "chrony", "ptp", "rtc", "pps", "system", "external"}
ESTIMATE_TYPE = "syn://type/clock-estimate"
STATS_TYPE = "syn://type/clock-source-statistics"


def paired_time():
    """Return realtime and a bracketed monotonic timestamp in integer ns."""
    before = time.monotonic_ns()
    wall = time.time_ns()
    after = time.monotonic_ns()
    return wall, (before+after)//2, (after-before)/2e6


def iso_ns(value):
    return datetime.fromtimestamp(value / 1e9, UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ClockReading:
    source: str
    kind: str
    systemNs: int
    monotonicNs: int
    offsetMs: float | None = None
    uncertaintyMs: float | None = None
    resolutionMs: float | None = None
    valid: bool = True
    reason: str | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.source, str) or not 1 <= len(self.source) <= 256:
            raise ValueError("Invalid clock source identifier")
        if self.kind not in KINDS or type(self.valid) is not bool:
            raise ValueError("Invalid clock kind or validity")
        for name in ("systemNs", "monotonicNs"):
            if type(getattr(self, name)) is not int or not 0 <= getattr(self, name) < 2**63:
                raise ValueError("Invalid clock epoch")
        for name in ("uncertaintyMs", "resolutionMs"):
            if getattr(self, name) is not None:
                number(getattr(self, name), name)
        if self.offsetMs is not None:
            number(self.offsetMs, "offsetMs", -1e15, 1e15)
        if self.reason is not None and (not isinstance(self.reason, str) or len(self.reason) > 256):
            raise ValueError("Invalid bounded clock reason")
        if not isinstance(self.metadata, dict):
            raise ValueError("Clock metadata must be a mapping")
        if len(json.dumps(self.metadata, allow_nan=False)) > 16384:
            raise ValueError("Oversized clock metadata")

    def to_dict(self):
        return {**asdict(self), "observedAt": iso_ns(self.systemNs),
                "typeRef": "syn://type/"+self.kind+"-clock-reading"}

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        value.pop("observedAt", None)
        value.pop("typeRef", None)
        return cls(**value)

    @classmethod
    def failure(cls, source, kind, reason, **metadata):
        wall, mono, _ = paired_time()
        return cls(source, kind, wall, mono, valid=False, reason=reason, metadata=metadata)
