"""Bounded values crossing the native Bluetooth/runtime boundary.

The byte maps retain bytes in memory and use lowercase hexadecimal strings in
JSON records. Constructors detach input containers so an OS callback cannot
subsequently change the data that the runtime received.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import math
import re
from typing import Any
from uuid import UUID


MAX_ADVERTISING_DATA_BYTES = 1_650
MAX_RECEIVED_DATA_BYTES = 8_192
MAX_METADATA_BYTES = 8_192
MAX_DATA_ENTRIES = 32
MAX_PORTABLE_JSON_NUMBER = (1 << 53) - 1
BLUETOOTH_UUID_SUFFIX = "-0000-1000-8000-00805f9b34fb"
_SHORT_UUID = re.compile(r"[0-9a-fA-F]{4}(?:[0-9a-fA-F]{4})?\Z")
_LONG_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


def _text(value: Any, label: str, *, maximum: int = 512,
          optional: bool = False, empty: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or (not empty and not value):
        raise ValueError(f"{label} must be a bounded string")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its byte limit")
    if "\0" in value:
        raise ValueError(f"{label} must not contain a NUL character")
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{label} must be valid Unicode") from None
    if length > maximum:
        raise ValueError(f"{label} exceeds its byte limit")
    return value


def canonical_uuid(value: str) -> str:
    """Normalize a Bluetooth 16/32-bit UUID or an exact 128-bit UUID."""
    if type(value) is not str or len(value) > 36:
        raise ValueError("Bluetooth UUID must be a 16, 32 or 128-bit UUID")
    if _SHORT_UUID.fullmatch(value):
        return f"{int(value, 16):08x}" + BLUETOOTH_UUID_SUFFIX
    if _LONG_UUID.fullmatch(value):
        return str(UUID(value))
    raise ValueError("Bluetooth UUID must be a 16, 32 or 128-bit UUID")


def _uuids(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or len(values) > MAX_DATA_ENTRIES:
        raise ValueError("Bluetooth service UUID list exceeds its bound")
    result = tuple(canonical_uuid(value) for value in values)
    if len(set(result)) != len(result):
        raise ValueError("Bluetooth service UUID list has duplicate UUIDs")
    return result


def _bytes(value: Any, maximum: int = MAX_ADVERTISING_DATA_BYTES) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)) or len(value) > maximum:
        raise ValueError("Bluetooth bytes exceed their bound")
    if isinstance(value, memoryview) and value.nbytes > maximum:
        raise ValueError("Bluetooth bytes exceed their bound")
    result = bytes(value)
    if len(result) > maximum:
        raise ValueError("Bluetooth bytes exceed their bound")
    return result


def _service_data(values: Any) -> dict[str, bytes]:
    if not isinstance(values, Mapping) or len(values) > MAX_DATA_ENTRIES:
        raise ValueError("Bluetooth service data exceeds its entry bound")
    result: dict[str, bytes] = {}
    for key, value in values.items():
        key = canonical_uuid(key)
        if key in result:
            raise ValueError("Bluetooth service data has duplicate UUIDs")
        result[key] = _bytes(value)
    return result


def _manufacturer_data(values: Any) -> dict[int, bytes]:
    if not isinstance(values, Mapping) or len(values) > MAX_DATA_ENTRIES:
        raise ValueError("Bluetooth manufacturer data exceeds its entry bound")
    result: dict[int, bytes] = {}
    for key, value in values.items():
        if type(key) is not int or not 0 <= key <= 0xFFFF:
            raise ValueError("Bluetooth company identifier must be uint16")
        result[key] = _bytes(value)
    return result


def bounded_json(value: Any, *, max_bytes: int = MAX_METADATA_BYTES) -> Any:
    """Return detached, finite JSON data with bounded size, depth and work."""
    remaining = 512

    def visit(item: Any, depth: int) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 6:
            raise ValueError("Bluetooth metadata exceeds its structural bound")
        if item is None or type(item) is bool:
            return item
        if type(item) is int:
            if not -MAX_PORTABLE_JSON_NUMBER <= item <= MAX_PORTABLE_JSON_NUMBER:
                raise ValueError("Bluetooth metadata integer exceeds its bound")
            return item
        if type(item) is float:
            if not math.isfinite(item) or abs(item) > MAX_PORTABLE_JSON_NUMBER:
                raise ValueError("Bluetooth metadata numbers must be finite and portable")
            return item
        if type(item) is str:
            return _text(item, "Bluetooth metadata string", maximum=max_bytes, empty=True)
        if isinstance(item, Mapping):
            if len(item) > 128:
                raise ValueError("Bluetooth metadata mapping exceeds its bound")
            result = {}
            for key, child in item.items():
                key = _text(key, "Bluetooth metadata key", maximum=128)
                result[key] = visit(child, depth + 1)
            return result
        if isinstance(item, (tuple, list)):
            if len(item) > 128:
                raise ValueError("Bluetooth metadata list exceeds its bound")
            return [visit(child, depth + 1) for child in item]
        raise ValueError("Bluetooth metadata must contain only JSON values")

    result = visit(value, 0)
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")) > max_bytes:
        raise ValueError("Bluetooth metadata exceeds its byte bound")
    return result


def _normalize_data(owner: Any) -> None:
    object.__setattr__(owner, "service_uuids", _uuids(owner.service_uuids))
    object.__setattr__(owner, "service_data", _service_data(owner.service_data))
    object.__setattr__(owner, "manufacturer_data", _manufacturer_data(owner.manufacturer_data))
    total = sum(len(value) for value in owner.service_data.values())
    total += sum(len(value) for value in owner.manufacturer_data.values())
    if total > MAX_RECEIVED_DATA_BYTES:
        raise ValueError("Bluetooth received data exceeds its total byte bound")


def _details(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Bluetooth details must be a JSON object")
    return bounded_json(value)


def _data_dict(owner: Any) -> dict[str, Any]:
    return {
        "serviceUuids": list(owner.service_uuids),
        "serviceData": {key: value.hex() for key, value in owner.service_data.items()},
        "manufacturerData": {str(key): value.hex()
                             for key, value in owner.manufacturer_data.items()},
    }


@dataclass(frozen=True)
class Adapter:
    key: str
    backend: str
    locator: str
    name: str
    connection_epoch: str
    can_advertise: bool
    can_scan: bool
    service_data: bool = True
    manufacturer_data: bool = True
    max_advertising_data_bytes: int = 31
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for key in ("key", "backend", "locator", "name", "connection_epoch"):
            _text(getattr(self, key), "Bluetooth adapter " + key)
        for key in ("can_advertise", "can_scan", "service_data", "manufacturer_data"):
            if type(getattr(self, key)) is not bool:
                raise ValueError("Bluetooth adapter capabilities must be boolean")
        if (type(self.max_advertising_data_bytes) is not int
                or not 0 <= self.max_advertising_data_bytes <= MAX_ADVERTISING_DATA_BYTES):
            raise ValueError("Bluetooth adapter advertising limit is out of range")
        object.__setattr__(self, "details", _details(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "backend": self.backend, "locator": self.locator,
            "name": self.name, "connectionEpoch": self.connection_epoch,
            "canAdvertise": self.can_advertise, "canScan": self.can_scan,
            "serviceData": self.service_data, "manufacturerData": self.manufacturer_data,
            "maxAdvertisingDataBytes": self.max_advertising_data_bytes,
            "details": _details(self.details),
        }


@dataclass(frozen=True)
class BeaconFrame:
    frame_id: str
    format: str
    service_uuids: tuple[str, ...] = ()
    service_data: dict[str, bytes] = field(default_factory=dict)
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)
    local_name: str | None = None
    advertising_data: bytes = b""

    def __post_init__(self) -> None:
        _text(self.frame_id, "Bluetooth frame identifier", maximum=128)
        _text(self.format, "Bluetooth frame format", maximum=64)
        _text(self.local_name, "Bluetooth local name", maximum=248, optional=True, empty=True)
        _normalize_data(self)
        object.__setattr__(self, "advertising_data", _bytes(self.advertising_data))

    def to_dict(self) -> dict[str, Any]:
        return {"frameId": self.frame_id, "format": self.format,
                **_data_dict(self), "localName": self.local_name,
                "advertisingData": self.advertising_data.hex()}


@dataclass(frozen=True)
class ReceivedAdvertisement:
    adapter_key: str
    source_id: str
    address: str | None = None
    name: str | None = None
    rssi: int | None = None
    service_uuids: tuple[str, ...] = ()
    service_data: dict[str, bytes] = field(default_factory=dict)
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.adapter_key, "Bluetooth receiving adapter")
        _text(self.source_id, "Bluetooth advertisement source")
        _text(self.address, "Bluetooth address", maximum=128, optional=True)
        _text(self.name, "Bluetooth received name", maximum=248, optional=True, empty=True)
        if self.rssi is not None and (type(self.rssi) is not int or not -127 <= self.rssi <= 127):
            raise ValueError("Bluetooth RSSI must be a signed byte or null")
        _normalize_data(self)
        if (not isinstance(self.observed_at, datetime)
                or self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None):
            raise ValueError("Bluetooth observation time must include its timezone")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))
        object.__setattr__(self, "details", _details(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {"adapterKey": self.adapter_key, "sourceId": self.source_id,
                "address": self.address, "name": self.name, "rssi": self.rssi,
                **_data_dict(self), "observedAt": self.observed_at.isoformat(),
                "details": _details(self.details)}
