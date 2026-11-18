"""Legacy-size Syn beacons and conservative, source-bound identification.

Every transmitted packet contains a fixed Syn marker. Native Syn service data
conveys a canonical Thing reference over bounded fragments. A checksum detects
accidental corruption; neither it nor a Bluetooth address authenticates a node.
Short standard-beacon hints never establish a reference on their own.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import math
import re
import time
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5
import zlib

from .models import (BLUETOOTH_UUID_SUFFIX, MAX_ADVERTISING_DATA_BYTES, BeaconFrame,
                     ReceivedAdvertisement, canonical_uuid)


SYN_BEACON_PROFILE = "syn.bluetooth-node-beacon/1"
SYN_SERVICE_PROFILE_URI = "syn://type/bluetooth-node-beacon-v1"
SYN_SERVICE_UUID = str(uuid5(NAMESPACE_URL, SYN_SERVICE_PROFILE_URI))
SYN_IBEACON_UUID = str(uuid5(NAMESPACE_URL, "syn://type/bluetooth-node-ibeacon-v1"))
SYN_IBEACON_FRAGMENT_PROFILE_URI = "syn://type/bluetooth-node-ibeacon-fragment-v1"
SYN_IBEACON_FRAGMENT_PREFIX = b"SYN\x01" + uuid5(
    NAMESPACE_URL, SYN_IBEACON_FRAGMENT_PROFILE_URI).bytes[:4]
SYN_EDDYSTONE_NAMESPACE = (b"SYN\x01" + hashlib.sha256(
    b"syn://type/bluetooth-node-eddystone-v1").digest()[:6])
SYN_ALTBEACON_MARKER = b"SYN\x01"
EDDYSTONE_SERVICE_UUID = canonical_uuid("feaa")
IBEACON_COMPANY_ID = 0x004C
LEGACY_ADVERTISING_DATA_BYTES = 31
MAX_NODE_REF_BYTES = 267  # "syn://thing/" plus a maximum 255-character Syn slug.
FRAGMENT_CHUNK_BYTES = 7
IBEACON_FRAGMENT_CHUNK_BYTES = 9
REFERENCE_ENVELOPE_OVERHEAD = 6  # uint16 length and CRC32, both big endian.
MAX_NODE_REF_FRAGMENTS = math.ceil(
    (MAX_NODE_REF_BYTES + REFERENCE_ENVELOPE_OVERHEAD) / FRAGMENT_CHUNK_BYTES)
MAX_IBEACON_NODE_REF_FRAGMENTS = math.ceil(
    (MAX_NODE_REF_BYTES + REFERENCE_ENVELOPE_OVERHEAD) / IBEACON_FRAGMENT_CHUNK_BYTES)
MIN_NODE_REF_FRAGMENTS = 3
FRAGMENT_VERSION = 1
DEFAULT_FORMATS = ("syn", "ibeacon", "eddystone-uid")
SUPPORTED_FORMATS = frozenset((*DEFAULT_FORMATS, "syn-ibeacon", "altbeacon"))
_CHECKSUM_DOMAIN = b"SYN-BEACON/1\0"
_NODE_REF = re.compile(r"(?:syn|synny)://thing/([a-z0-9]+(?:[-_][a-z0-9]+)*)\Z")


class UnsupportedBeaconFormat(ValueError):
    """This runtime cannot send the requested format with an explicit Syn marker."""


def canonical_node_ref(value: str) -> str:
    if type(value) is not str or len(value) > MAX_NODE_REF_BYTES + 2:
        raise ValueError("Syn beacon node reference exceeds its byte bound")
    match = _NODE_REF.fullmatch(value)
    if match is None or len(match[1]) > 255:
        raise ValueError("Syn beacon identity must be a canonical Syn Thing reference")
    result = "syn://thing/" + match[1]
    if len(result.encode("ascii")) > MAX_NODE_REF_BYTES:
        raise ValueError("Syn beacon node reference exceeds its byte bound")
    return result


def uuid_wire_bytes(value: str) -> bytes:
    """Bluetooth UUID128 little-endian wire order (not Microsoft GUID order)."""
    return UUID(canonical_uuid(value)).bytes[::-1]


def _uuid_bytes(value: str) -> tuple[int, bytes]:
    value = canonical_uuid(value)
    if value.endswith(BLUETOOTH_UUID_SUFFIX):
        number = int(value[:8], 16)
        width = 2 if number <= 0xFFFF else 4
        return width, number.to_bytes(width, "little")
    return 16, uuid_wire_bytes(value)


def _ad(kind: int, value: bytes) -> bytes:
    if len(value) > 254:
        raise ValueError("Bluetooth AD element exceeds its length byte")
    return bytes((len(value) + 1, kind)) + value


def serialize_advertising_data(*, service_uuids: tuple[str, ...] = (),
                               service_data: Mapping[str, bytes] | None = None,
                               manufacturer_data: Mapping[int, bytes] | None = None,
                               local_name: str | None = None,
                               include_flags: bool = True) -> bytes:
    """Serialize the structured fields, reserving Flags in the legacy budget.

    Native APIs may supply their own Flags; these planned bytes describe the
    payload and conservative budget, not a claim of an over-the-air capture.
    """
    if type(include_flags) is not bool:
        raise ValueError("Bluetooth Flags selection must be boolean")
    normalized = BeaconFrame("serialize", "generic", service_uuids,
                             {} if service_data is None else service_data,
                             {} if manufacturer_data is None else manufacturer_data,
                             local_name)
    parts = [b"\x02\x01\x06"] if include_flags else []
    for width, kind in ((2, 0x03), (4, 0x05), (16, 0x07)):
        values = [_uuid_bytes(value)[1] for value in normalized.service_uuids
                  if _uuid_bytes(value)[0] == width]
        if values:
            parts.append(_ad(kind, b"".join(values)))
    for key, value in normalized.service_data.items():
        width, wire_uuid = _uuid_bytes(key)
        parts.append(_ad({2: 0x16, 4: 0x20, 16: 0x21}[width], wire_uuid + value))
    for key, value in normalized.manufacturer_data.items():
        parts.append(_ad(0xFF, key.to_bytes(2, "little") + value))
    if local_name is not None:
        parts.append(_ad(0x09, local_name.encode("utf-8")))
    result = b"".join(parts)
    if len(result) > MAX_ADVERTISING_DATA_BYTES:
        raise ValueError("Bluetooth advertisement exceeds its total byte bound")
    return result


def parse_advertising_data(raw: bytes) -> tuple[tuple[int, bytes], ...]:
    """Parse complete AD elements; allow only all-zero trailing padding."""
    if type(raw) is not bytes or len(raw) > MAX_ADVERTISING_DATA_BYTES:
        raise ValueError("Bluetooth advertisement exceeds its total byte bound")
    result = []
    offset = 0
    while offset < len(raw):
        length = raw[offset]
        if length == 0:
            if any(raw[offset:]):
                raise ValueError("Bluetooth advertisement has nonzero data after padding")
            break
        end = offset + 1 + length
        if end > len(raw):
            raise ValueError("Bluetooth advertisement has a truncated AD element")
        result.append((raw[offset + 1], raw[offset + 2:end]))
        offset = end
    return tuple(result)


def _frame(frame_id: str, format: str, *, service_uuids: tuple[str, ...] = (),
           service_data: dict[str, bytes] | None = None,
           manufacturer_data: dict[int, bytes] | None = None) -> BeaconFrame:
    service_data = service_data or {}
    manufacturer_data = manufacturer_data or {}
    raw = serialize_advertising_data(service_uuids=service_uuids,
                                    service_data=service_data,
                                    manufacturer_data=manufacturer_data)
    if len(raw) > LEGACY_ADVERTISING_DATA_BYTES:
        raise ValueError("Syn beacon exceeds the legacy 31-byte advertising limit")
    return BeaconFrame(frame_id, format, service_uuids, service_data,
                       manufacturer_data, advertising_data=raw)


def build_frames(node_ref: str, formats: tuple[str, ...] = DEFAULT_FORMATS, *,
                 company_id: int | None = None) -> tuple[BeaconFrame, ...]:
    """Build deterministic, individually Syn-branded legacy advertisements.

    Standard beacon power bytes are nominal defaults, not measured calibration:
    iBeacon/AltBeacon -59 dBm at one metre, Eddystone -18 dBm at zero metres.
    Their truncated SHA-256 hints cannot establish a node reference by themselves.
    """
    node_ref = canonical_node_ref(node_ref)
    if not isinstance(formats, (tuple, list)) or len(formats) > len(SUPPORTED_FORMATS):
        raise ValueError("Syn beacon formats must be a bounded sequence")
    if any(type(value) is not str or value not in SUPPORTED_FORMATS for value in formats):
        raise UnsupportedBeaconFormat(
            "Runtime supports Syn, Syn iBeacon identity, iBeacon, Eddystone UID and configured AltBeacon")
    if len(set(formats)) != len(formats):
        raise ValueError("Syn beacon formats must not contain duplicates")
    if company_id is not None and (type(company_id) is not int or not 0 <= company_id <= 0xFFFF):
        raise ValueError("AltBeacon company identifier must be an explicitly configured uint16")
    reference = node_ref.encode("ascii")
    digest = hashlib.sha256(reference).digest()
    result = []
    for format in formats:
        if format in ("syn", "syn-ibeacon"):
            envelope = len(reference).to_bytes(2, "big") + reference
            payload = envelope + zlib.crc32(_CHECKSUM_DOMAIN + envelope).to_bytes(4, "big")
            chunk_bytes = FRAGMENT_CHUNK_BYTES if format == "syn" else IBEACON_FRAGMENT_CHUNK_BYTES
            count = math.ceil(len(payload) / chunk_bytes)
            token = hashlib.sha256(payload).digest()[0]
            for index in range(count):
                chunk = payload[index * chunk_bytes:(index + 1) * chunk_bytes]
                if format == "syn-ibeacon":
                    chunk = chunk.ljust(chunk_bytes, b"\0")
                body = bytes(((FRAGMENT_VERSION << 6) | index, count, token)) + chunk
                if format == "syn":
                    result.append(_frame(f"syn-{index:02d}", "syn",
                                         service_data={SYN_SERVICE_UUID: body}))
                else:
                    # Prefix8 + body12 occupy UUID16 and major/minor4. The
                    # iBeacon framing and power byte remain standard.
                    data = b"\x02\x15" + SYN_IBEACON_FRAGMENT_PREFIX + body + b"\xc5"
                    result.append(_frame(f"syn-ibeacon-{index:02d}", "syn-ibeacon",
                                         manufacturer_data={IBEACON_COMPANY_ID: data}))
        elif format == "ibeacon":
            body = b"\x02\x15" + UUID(SYN_IBEACON_UUID).bytes + digest[:4] + b"\xc5"
            result.append(_frame(format, format, manufacturer_data={IBEACON_COMPANY_ID: body}))
        elif format == "eddystone-uid":
            body = b"\x00\xee" + SYN_EDDYSTONE_NAMESPACE + digest[:6] + b"\x00\x00"
            result.append(_frame(format, format, service_uuids=(EDDYSTONE_SERVICE_UUID,),
                                 service_data={EDDYSTONE_SERVICE_UUID: body}))
        elif format == "altbeacon":
            if company_id is None:
                raise UnsupportedBeaconFormat("AltBeacon requires the operator's assigned Bluetooth company identifier")
            body = b"\xbe\xac" + SYN_ALTBEACON_MARKER + digest[:16] + b"\xc5\x00"
            result.append(_frame(format, format, manufacturer_data={company_id: body}))
    return tuple(result)


@dataclass
class _Assembly:
    count: int
    expires: float
    chunks: dict[int, bytes] = field(default_factory=dict)


def _identity(*, is_syn: bool = False, format: str = "bluetooth-advertisement",
              reference: str | None = None, hint: bytes | None = None,
              evidence: str = "none", **extra: Any) -> dict[str, Any]:
    return {"isSyn": is_syn, "format": format, "claimedNodeRef": reference,
            "identityHint": None if hint is None else {
                "kind": "node-ref-sha256-prefix", "hex": hint.hex(), "bits": len(hint) * 8},
            "identityEvidence": evidence, "authenticated": False, **extra}


class BeaconDecoder:
    """Bounded fragment and identity cache, partitioned by adapter and source.

    ``source_id`` must be a stable identifier for this native observation stream.
    Bluetooth address rotation starts a new stream; state is never joined by a
    short identity hint. Expiry is measured from the first fragment, so repeats
    cannot indefinitely retain an incomplete assembly.
    """

    def __init__(self, ttl_seconds: float = 120, max_assemblies: int = 128):
        if (type(ttl_seconds) not in (int, float) or not 0 < ttl_seconds <= 3_600
                or not math.isfinite(ttl_seconds)):
            raise ValueError("Syn beacon assembly TTL must be in (0, 3600] seconds")
        if type(max_assemblies) is not int or not 1 <= max_assemblies <= 4_096:
            raise ValueError("Syn beacon assembly bound must be in [1, 4096]")
        self.ttl_seconds = float(ttl_seconds)
        self.max_assemblies = max_assemblies
        self._assemblies: OrderedDict[tuple[str, str, str, int], _Assembly] = OrderedDict()
        self._identities: OrderedDict[tuple[str, str], tuple[str, bytes, float]] = OrderedDict()
        self._last_now: float | None = None

    def forget_adapter(self, adapter_key: str) -> None:
        """Discard receiver-session identity state after detach or reattach."""
        for key in [key for key in self._assemblies if key[0] == adapter_key]:
            del self._assemblies[key]
        for key in [key for key in self._identities if key[0] == adapter_key]:
            del self._identities[key]

    def _expire(self, now: float) -> None:
        if self._last_now is not None and now < self._last_now:
            self._assemblies.clear()
            self._identities.clear()
        self._last_now = now
        for key in [key for key, value in self._assemblies.items() if value.expires <= now]:
            del self._assemblies[key]
        for key in [key for key, value in self._identities.items() if value[2] <= now]:
            del self._identities[key]

    def _fragment(self, source: tuple[str, str], body: bytes, now: float, *,
                  format: str = "syn") -> dict[str, Any]:
        fixed = format == "syn-ibeacon"
        chunk_bytes = IBEACON_FRAGMENT_CHUNK_BYTES if fixed else FRAGMENT_CHUNK_BYTES
        max_fragments = MAX_IBEACON_NODE_REF_FRAGMENTS if fixed else MAX_NODE_REF_FRAGMENTS
        invalid = _identity(is_syn=True, format=format, evidence="malformed-fragment")
        if (not 4 <= len(body) <= 3 + chunk_bytes
                or (fixed and len(body) != 3 + chunk_bytes)
                or body[0] >> 6 != FRAGMENT_VERSION):
            return invalid
        index, count, token = body[0] & 0x3F, body[1], body[2]
        chunk = body[3:]
        if (not MIN_NODE_REF_FRAGMENTS <= count <= max_fragments
                or index >= count or (index < count - 1 and len(chunk) != chunk_bytes)):
            return invalid
        key = (*source, format, token)
        assembly = self._assemblies.get(key)
        if assembly is not None and (assembly.count != count
                                     or (index in assembly.chunks and assembly.chunks[index] != chunk)):
            del self._assemblies[key]
            self._identities.pop(source, None)
            return _identity(is_syn=True, format=format, evidence="conflicting-fragments")
        if assembly is None:
            while len(self._assemblies) >= self.max_assemblies:
                self._assemblies.popitem(last=False)
            assembly = _Assembly(count, now + self.ttl_seconds)
            self._assemblies[key] = assembly
        assembly.chunks[index] = chunk
        self._assemblies.move_to_end(key)
        if len(assembly.chunks) != count:
            return _identity(is_syn=True, format=format, evidence="partial-reference",
                             fragmentsReceived=len(assembly.chunks), fragmentsExpected=count)
        del self._assemblies[key]
        payload = b"".join(assembly.chunks[index] for index in range(count))
        ref_length = int.from_bytes(payload[:2], "big")
        payload_length = ref_length + REFERENCE_ENVELOPE_OVERHEAD
        expected_count = math.ceil(payload_length / chunk_bytes)
        if (not 13 <= ref_length <= MAX_NODE_REF_BYTES or count != expected_count
                or len(payload) < payload_length or any(payload[payload_length:])
                or (not fixed and len(payload) != payload_length)):
            self._identities.pop(source, None)
            return _identity(is_syn=True, format=format, evidence="invalid-reference-length")
        payload = payload[:payload_length]
        reference, checksum = payload[2:-4], payload[-4:]
        if (hashlib.sha256(payload).digest()[0] != token
                or zlib.crc32(_CHECKSUM_DOMAIN + payload[:-4]).to_bytes(4, "big") != checksum):
            self._identities.pop(source, None)
            return _identity(is_syn=True, format=format, evidence="invalid-reference-checksum")
        try:
            decoded = reference.decode("ascii")
            canonical = canonical_node_ref(decoded)
            if canonical != decoded:
                raise ValueError("Non-canonical reference on wire")
        except (UnicodeError, ValueError):
            self._identities.pop(source, None)
            return _identity(is_syn=True, format=format, evidence="invalid-reference")
        self._identities[source] = (canonical, hashlib.sha256(reference).digest(), now + self.ttl_seconds)
        self._identities.move_to_end(source)
        while len(self._identities) > self.max_assemblies:
            self._identities.popitem(last=False)
        return _identity(is_syn=True, format=format, reference=canonical,
                         evidence="reassembled-reference")

    def _hint(self, source: tuple[str, str], format: str, hint: bytes) -> dict[str, Any]:
        cached = self._identities.get(source)
        if cached is not None and cached[1].startswith(hint):
            return _identity(is_syn=True, format=format, reference=cached[0], hint=hint,
                             evidence="source-bound-reference-hint")
        return _identity(is_syn=True, format=format, hint=hint, evidence="short-hint-only")

    def observe(self, advertisement: ReceivedAdvertisement, *,
                now_monotonic: float | None = None) -> dict[str, Any]:
        """Classify one native observation; malformed input never escapes here."""
        try:
            if not isinstance(advertisement, ReceivedAdvertisement):
                return _identity(evidence="malformed-advertisement")
            now = time.monotonic() if now_monotonic is None else now_monotonic
            if type(now) not in (int, float) or not math.isfinite(now):
                return _identity(evidence="invalid-monotonic-time")
            self._expire(now)
            # Revalidate detached maps: dataclass attributes are frozen but their
            # public mapping fields may have been modified by a caller.
            advertisement = ReceivedAdvertisement(
                advertisement.adapter_key, advertisement.source_id,
                advertisement.address, advertisement.name, advertisement.rssi,
                advertisement.service_uuids, advertisement.service_data,
                advertisement.manufacturer_data, advertisement.observed_at,
                advertisement.details)
            source = (advertisement.adapter_key, advertisement.source_id)
            candidates: list[dict[str, Any]] = []
            native = advertisement.service_data.get(SYN_SERVICE_UUID)
            if native is not None:
                candidates.append(self._fragment(source, native, now))
            elif SYN_SERVICE_UUID in advertisement.service_uuids:
                candidates.append(_identity(is_syn=True, format="syn", evidence="syn-service-marker"))
            for company, body in advertisement.manufacturer_data.items():
                if company == IBEACON_COMPANY_ID and body.startswith(b"\x02\x15"):
                    if body[2:10] == SYN_IBEACON_FRAGMENT_PREFIX:
                        if len(body) == 23:
                            candidates.append(self._fragment(source, body[10:22], now,
                                                             format="syn-ibeacon"))
                        else:
                            candidates.append(_identity(is_syn=True, format="syn-ibeacon",
                                                        evidence="malformed-fragment"))
                        continue
                    is_syn = body[2:18] == UUID(SYN_IBEACON_UUID).bytes
                    if is_syn and len(body) == 23:
                        candidates.append(self._hint(source, "ibeacon", body[18:22]))
                    else:
                        candidates.append(_identity(is_syn=is_syn, format="ibeacon",
                                                    evidence="malformed-beacon" if is_syn else "none"))
                elif body.startswith(b"\xbe\xac"):
                    is_syn = body[2:6] == SYN_ALTBEACON_MARKER
                    if is_syn and len(body) == 24:
                        candidates.append(self._hint(source, "altbeacon", body[6:22]))
                    else:
                        candidates.append(_identity(is_syn=is_syn, format="altbeacon",
                                                    evidence="malformed-beacon" if is_syn else "none"))
            body = advertisement.service_data.get(EDDYSTONE_SERVICE_UUID)
            if body:
                format = {0x00: "eddystone-uid", 0x10: "eddystone-url", 0x20: "eddystone-tlm",
                          0x30: "eddystone-eid"}.get(body[0], "eddystone")
                is_syn = body[0] == 0 and body[2:12] == SYN_EDDYSTONE_NAMESPACE
                if is_syn and len(body) == 20 and body[-2:] == b"\x00\x00":
                    candidates.append(self._hint(source, format, body[12:18]))
                else:
                    candidates.append(_identity(is_syn=is_syn, format=format,
                                                evidence="malformed-beacon" if is_syn else "none"))
            syn = [result for result in candidates if result["isSyn"]]
            resolved = [result for result in syn if result["claimedNodeRef"] is not None]
            if resolved:
                result = resolved[0]
                digest = hashlib.sha256(result["claimedNodeRef"].encode("ascii")).hexdigest()
                if any(candidate["identityHint"] is not None
                       and not digest.startswith(candidate["identityHint"]["hex"]) for candidate in syn):
                    self._identities.pop(source, None)
                    return _identity(is_syn=True, format="syn", evidence="conflicting-identity-hints")
                return result
            return (syn or candidates or [_identity()])[0]
        except Exception:
            return _identity(evidence="malformed-advertisement")
