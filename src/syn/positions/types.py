"""Versioned position values, independent of devices, storage and scheduling.

WGS84 geographic-geodetic positions use latitude/longitude angles and an
explicitly qualified altitude, not Cartesian X/Y/Z. The legacy type identifier
is read only for immutable historical records; all new positions use GEOGRAPHIC.
Uncertainty is a qualified estimate, never an assertion of guaranteed coverage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import math
from typing import Any, Mapping
from types import MappingProxyType

POSITION_ESTIMATE = "syn://type/space-position-estimate"
GEOGRAPHIC = "syn://type/geographic-geodetic"
# Compatibility for immutable records written before the terminology correction.
LEGACY_GEOGRAPHIC = "syn://type/geographic-cartesian"
GPS_READING = "syn://type/gps-reading"
OS_READING = "syn://type/os-location-reading"
GEOGRAPHIC_SPACE = "syn://thing/space-wgs84-geodetic"
EARTH_RADIUS = 6_371_008.8


def canonical(value: Any) -> bytes:
    def normalize(v):
        if isinstance(v, float) and math.isfinite(v) and v.is_integer():
            return int(v)  # JSONB normalizes -0; hash the same numeric value.
        if isinstance(v, Mapping):
            return {key: normalize(child) for key, child in v.items()}
        if isinstance(v, (tuple, list)):
            return [normalize(child) for child in v]
        return v
    return json.dumps(normalize(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def finite(value: Any, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"Invalid {name}") from error
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"Invalid {name}")
    return number


def timestamp(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Position timestamps must include a UTC offset")
    return value.astimezone(UTC)


def local_ref(value: str, kind: str) -> str:
    import re
    if not isinstance(value, str) or not re.fullmatch(rf"syn://{kind}/[a-z0-9][a-z0-9-]{{0,254}}", value):
        raise ValueError(f"Expected a local syn://{kind}/ reference")
    return value


def detached(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = canonical(value)
    if len(raw) > 32_768:
        raise ValueError("Position metadata exceeds 32768 bytes")
    return json.loads(raw)


def frozen(value: Any) -> Any:
    """Deep-freeze detached metadata; callers cannot mutate a hashed value."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: frozen(child) for key, child in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(frozen(child) for child in value)
    return value


@dataclass(frozen=True)
class Space:
    reference: str
    typeRef: str
    axes: tuple[str, ...]
    units: tuple[str, ...]
    errorUnit: str
    metric: str = "unspecified"
    description: str = ""

    def __post_init__(self):
        object.__setattr__(self, "axes", tuple(self.axes))
        object.__setattr__(self, "units", tuple(self.units))
        local_ref(self.reference, "thing")
        local_ref(self.typeRef, "type")
        if (not 1 <= len(self.axes) <= 64 or len(set(self.axes)) != len(self.axes)
                or len(self.axes) != len(self.units)):
            raise ValueError("A space needs 1..64 unique axes and one unit per axis")
        if any(not isinstance(x, str) or not 1 <= len(x) <= 128 for x in (*self.axes, *self.units, self.errorUnit)):
            raise ValueError("Invalid space axes or units")
        if self.typeRef in {POSITION_ESTIMATE, GPS_READING, OS_READING, LEGACY_GEOGRAPHIC}:
            raise ValueError("A space must have a current concrete position type, not an observation or legacy type")
        if self.reference == GEOGRAPHIC_SPACE and self.typeRef != GEOGRAPHIC:
            raise ValueError("The WGS84 space has a fixed type")
        if self.typeRef == GEOGRAPHIC and (self.reference != GEOGRAPHIC_SPACE
                or self.axes != ("latitude", "longitude", "altitude")
                or self.units != ("degree", "degree", "metre") or self.errorUnit != "metre"):
            raise ValueError("Invalid WGS84 space definition")

    def to_dict(self):
        return {"reference": self.reference, "typeRef": self.typeRef,
                "axes": list(self.axes), "units": list(self.units), "errorUnit": self.errorUnit,
                "metric": self.metric, "description": self.description}


WGS84 = Space(GEOGRAPHIC_SPACE, GEOGRAPHIC, ("latitude", "longitude", "altitude"),
              ("degree", "degree", "metre"), "metre", "wgs84-horizontal",
              "WGS84 geodetic position; altitude datum is specified by each estimate")


@dataclass(frozen=True)
class Estimate:
    spaceRef: str
    typeRef: str
    coordinates: Mapping[str, float | None]
    error: float | None
    errorUnit: str
    observedAt: datetime
    source: str
    uncertaintyMethod: str = "unknown"
    confidenceLevel: float | None = None
    verticalError: float | None = None
    altitudeDatum: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        local_ref(self.spaceRef, "thing")
        local_ref(self.typeRef, "type")
        object.__setattr__(self, "observedAt", timestamp(self.observedAt))
        values = dict(self.coordinates)
        if not 1 <= len(values) <= 64:
            raise ValueError("Position must contain 1..64 coordinates")
        for name, value in values.items():
            if not isinstance(name, str) or not 1 <= len(name) <= 128:
                raise ValueError("Invalid axis name")
            values[name] = None if value is None else finite(value, name)
        object.__setattr__(self, "coordinates", MappingProxyType(values))
        object.__setattr__(self, "provenance", frozen(detached(self.provenance)))
        for attr in ("error", "verticalError"):
            value = getattr(self, attr)
            if value is not None:
                object.__setattr__(self, attr, finite(value, attr, minimum=0))
        if self.confidenceLevel is not None and not 0 < finite(self.confidenceLevel, "confidenceLevel") <= 1:
            raise ValueError("confidenceLevel must be in (0,1]")
        if any(not isinstance(value, str) or not 1 <= len(value) <= 512
               for value in (self.source, self.errorUnit, self.uncertaintyMethod)):
            raise ValueError("Source, error unit and uncertainty method are required")
        if self.spaceRef == GEOGRAPHIC_SPACE:
            if set(values) != {"latitude", "longitude", "altitude"} or self.errorUnit != "metre":
                raise ValueError("WGS84 requires latitude, longitude, altitude and metre error")
            lat, lon = values["latitude"], values["longitude"]
            if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError("Latitude/longitude outside WGS84 bounds")
            if values["altitude"] is not None and self.altitudeDatum not in {"ellipsoid", "mean-sea-level", "unknown"}:
                raise ValueError("An altitude requires its datum")

    def validate_space(self, space: Space) -> None:
        if (self.spaceRef != space.reference or self.typeRef != space.typeRef
                or set(self.coordinates) != set(space.axes) or self.errorUnit != space.errorUnit):
            raise ValueError("Estimate does not match its registered coordinate space")

    def to_dict(self) -> dict[str, Any]:
        result = {"schemaVersion": 1, "spaceRef": self.spaceRef, "typeRef": self.typeRef,
                  "coordinates": dict(self.coordinates), "error": self.error, "errorUnit": self.errorUnit,
                  "observedAt": self.observedAt.isoformat(), "source": self.source,
                  "uncertaintyMethod": self.uncertaintyMethod, "confidenceLevel": self.confidenceLevel,
                  "verticalError": self.verticalError, "altitudeDatum": self.altitudeDatum,
                  "provenance": dict(self.provenance)}
        if self.spaceRef == GEOGRAPHIC_SPACE:
            result.update(self.coordinates)
            result["errorMeters"] = self.error
        return detached(result)

    @classmethod
    def from_dict(cls, body: Mapping[str, Any]) -> Estimate:
        keys = ("spaceRef", "typeRef", "coordinates", "error", "errorUnit", "observedAt", "source",
                "uncertaintyMethod", "confidenceLevel", "verticalError", "altitudeDatum", "provenance")
        return cls(**{key: body[key] for key in keys})

    def belief(self) -> dict[str, Any]:
        """Timestamp/telemetry changes alone do not create a different position."""
        value = self.to_dict()
        for key in ("observedAt", "provenance"):
            value.pop(key)
        return value


@dataclass(frozen=True)
class Reading:
    deviceId: str
    observedAt: datetime
    receivedAt: datetime
    estimate: Estimate | None
    fix: str
    model: str | None = None
    serialNumber: str | None = None
    satellitesLocked: int | None = None
    satellitesVisible: int | None = None
    timecode: str | None = None
    telemetry: Mapping[str, Any] = field(default_factory=dict)
    typeRef: str = GPS_READING

    def __post_init__(self):
        for name in ("observedAt", "receivedAt"):
            object.__setattr__(self, name, timestamp(getattr(self, name)))
        if (not isinstance(self.deviceId, str) or not 1 <= len(self.deviceId) <= 512
                or self.fix not in {"none", "2d", "3d", "estimated"}):
            raise ValueError("Invalid device or fix status")
        if self.typeRef not in {GPS_READING, OS_READING}:
            raise ValueError("Unknown reading type")
        if self.fix == "none" and self.estimate is not None:
            raise ValueError("A no-fix observation cannot carry a position estimate")
        if self.estimate is not None and (self.estimate.spaceRef != GEOGRAPHIC_SPACE
                or self.estimate.observedAt != self.observedAt):
            raise ValueError("Reading and estimate must describe the same WGS84 epoch")
        for name in ("model", "serialNumber", "timecode"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or len(value) > 512):
                raise ValueError(f"Invalid {name}")
        for name in ("satellitesLocked", "satellitesVisible"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= 4096):
                raise ValueError("Invalid satellite count")
        object.__setattr__(self, "telemetry", frozen(detached(self.telemetry)))

    def to_dict(self) -> dict[str, Any]:
        value = self.estimate.to_dict() if self.estimate else {
            "schemaVersion": 1, "spaceRef": GEOGRAPHIC_SPACE,
            "latitude": None, "longitude": None, "altitude": None, "errorMeters": None,
            "coordinates": {"latitude": None, "longitude": None, "altitude": None},
            "error": None, "errorUnit": "metre", "source": self.deviceId,
            "uncertaintyMethod": "unknown", "confidenceLevel": None,
            "verticalError": None, "altitudeDatum": None, "provenance": {}}
        value.update(typeRef=self.typeRef, deviceId=self.deviceId,
                     observedAt=self.observedAt.isoformat(), receivedAt=self.receivedAt.isoformat(),
                     fix=self.fix, model=self.model, serialNumber=self.serialNumber,
                     satellitesLocked=self.satellitesLocked, satellitesVisible=self.satellitesVisible,
                     timecode=self.timecode, telemetry=dict(self.telemetry))
        return detached(value)

    @property
    def sampleKey(self) -> str:
        # Re-delivery of the same instrument sample is idempotent, arrival time is not its identity.
        value = self.to_dict()
        value.pop("receivedAt")
        return digest(value)


def geographic(latitude, longitude, altitude, error, observedAt, source, **kwargs) -> Estimate:
    return Estimate(GEOGRAPHIC_SPACE, GEOGRAPHIC,
                    {"latitude": latitude, "longitude": longitude, "altitude": altitude},
                    error, "metre", observedAt, source, **kwargs)


def horizontal_distance(a: Estimate, b: Estimate) -> float:
    if a.spaceRef != GEOGRAPHIC_SPACE or b.spaceRef != GEOGRAPHIC_SPACE:
        raise ValueError("A geographic distance is not defined for arbitrary spaces")
    lat1, lat2 = (math.radians(x.coordinates["latitude"]) for x in (a, b))
    dl = math.radians(b.coordinates["longitude"] - a.coordinates["longitude"])
    d = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dl/2)**2
    return 2 * EARTH_RADIUS * math.asin(math.sqrt(min(1.0, max(0.0, d))))


def choose_estimate(old: Estimate | None, new: Estimate, *, now: datetime,
                    lastObservedAt: datetime | None = None, maxAgeSeconds: float = 120,
                    futureToleranceSeconds: float = 5, driftPerSecond: float = 2) -> str:
    """Return a stable auditable decision. Never average correlated GPS reports.

    A less precise new position is usable when outside the old uncertainty disk
    or when the aged old disk has grown beyond it. Unknown accuracy is not zero.
    All accepted observations remain events even when they do not change belief.
    """
    age = (timestamp(now) - new.observedAt).total_seconds()
    if age < -futureToleranceSeconds:
        return "future"
    if age > maxAgeSeconds and new.source != "timezone":
        return "stale"
    if old is None:
        return "replace"
    if (old.spaceRef, old.typeRef, old.errorUnit) != (new.spaceRef, new.typeRef, new.errorUnit):
        raise ValueError("Cannot reconcile different coordinate spaces")
    latest = timestamp(lastObservedAt) if lastObservedAt else old.observedAt
    if new.observedAt < latest:
        return "out-of-order"
    if old.belief() == new.belief():
        return "unchanged"
    if old.source == "timezone" and new.source != "timezone":
        return "replace"
    if new.source == "timezone":
        return "bootstrap-only"
    if old.spaceRef == GEOGRAPHIC_SPACE:
        if (new.observedAt == latest and old.coordinates["altitude"] is not None
                and new.coordinates["altitude"] is None and horizontal_distance(old, new) < .01):
            return "less-complete"
        elapsed = max(0, (new.observedAt-latest).total_seconds())
        aged = None if old.error is None else old.error + elapsed * driftPerSecond
        if aged is not None and (new.error is None or new.error > aged):
            if horizontal_distance(old, new) <= aged:
                return "less-precise"
    return "replace"
