from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
import hashlib
import json
from uuid import NAMESPACE_URL, UUID, uuid5
import zlib

import pytest

from syn.bluetooth.models import (Adapter, BeaconFrame, ReceivedAdvertisement,
                                  bounded_json, canonical_uuid)
from syn.bluetooth.protocol import (
    BeaconDecoder, DEFAULT_FORMATS, EDDYSTONE_SERVICE_UUID, FRAGMENT_CHUNK_BYTES,
    IBEACON_COMPANY_ID, MAX_IBEACON_NODE_REF_FRAGMENTS, MAX_NODE_REF_BYTES,
    MAX_NODE_REF_FRAGMENTS, SYN_ALTBEACON_MARKER, SYN_EDDYSTONE_NAMESPACE,
    SYN_IBEACON_FRAGMENT_PREFIX, SYN_IBEACON_UUID, SYN_SERVICE_PROFILE_URI,
    SYN_SERVICE_UUID, UnsupportedBeaconFormat, build_frames, canonical_node_ref,
    parse_advertising_data, serialize_advertising_data, uuid_wire_bytes,
)


NODE = "syn://thing/node-0123456789abcdef"


def received(frame: BeaconFrame, *, adapter="hci0", source="aa:bb:cc:dd:ee:ff",
             **kwargs) -> ReceivedAdvertisement:
    return ReceivedAdvertisement(adapter, source, service_uuids=frame.service_uuids,
                                 service_data=frame.service_data,
                                 manufacturer_data=frame.manufacturer_data, **kwargs)


def observe_frames(decoder, frames, *, now=0, **kwargs):
    return [decoder.observe(received(frame, **kwargs), now_monotonic=now) for frame in frames]


def body(frame):
    if frame.format == "syn":
        return frame.service_data[SYN_SERVICE_UUID]
    return frame.manufacturer_data[IBEACON_COMPANY_ID][10:22]


def changed_body(frame, data):
    if frame.format == "syn":
        return replace(frame, service_data={SYN_SERVICE_UUID: data})
    return replace(frame, manufacturer_data={IBEACON_COMPANY_ID:
        b"\x02\x15" + SYN_IBEACON_FRAGMENT_PREFIX + data + b"\xc5"})


def test_project_markers_are_deterministic_not_fabricated_sig_uuids():
    assert SYN_SERVICE_UUID == str(uuid5(NAMESPACE_URL, SYN_SERVICE_PROFILE_URI))
    assert UUID(SYN_SERVICE_UUID).version == 5
    assert len(SYN_IBEACON_FRAGMENT_PREFIX) == 8
    assert SYN_IBEACON_FRAGMENT_PREFIX.startswith(b"SYN\x01")
    assert len(SYN_EDDYSTONE_NAMESPACE) == 10
    assert SYN_EDDYSTONE_NAMESPACE.startswith(b"SYN\x01")
    assert SYN_ALTBEACON_MARKER == b"SYN\x01"


def test_exact_legacy_layouts_and_all_frames_have_a_syn_marker():
    formats = (*DEFAULT_FORMATS, "syn-ibeacon", "altbeacon")
    frames = build_frames(NODE, formats, company_id=0x1234)
    assert frames == build_frames(NODE, formats, company_id=0x1234)
    for frame in frames:
        assert 0 < len(frame.advertising_data) <= 31
        elements = parse_advertising_data(frame.advertising_data)
        assert elements[0] == (0x01, b"\x06")
        assert BeaconDecoder().observe(received(frame))["isSyn"]
        assert serialize_advertising_data(service_uuids=frame.service_uuids,
            service_data=frame.service_data,
            manufacturer_data=frame.manufacturer_data) == frame.advertising_data
    ibeacon = next(frame for frame in frames if frame.format == "ibeacon")
    assert len(ibeacon.advertising_data) == 30
    assert parse_advertising_data(ibeacon.advertising_data)[1] == (
        0xFF, b"\x4c\x00\x02\x15" + UUID(SYN_IBEACON_UUID).bytes
        + hashlib.sha256(NODE.encode()).digest()[:4] + b"\xc5")
    uid = next(frame for frame in frames if frame.format == "eddystone-uid")
    assert len(uid.advertising_data) == 31
    assert parse_advertising_data(uid.advertising_data)[1] == (0x03, b"\xaa\xfe")
    assert uid.service_data[EDDYSTONE_SERVICE_UUID][-2:] == b"\0\0"
    assert len(next(frame for frame in frames if frame.format == "altbeacon").advertising_data) == 31
    assert all(len(frame.advertising_data) == 30 for frame in frames if frame.format == "syn-ibeacon")


@pytest.mark.parametrize("format", ["syn", "syn-ibeacon"])
@pytest.mark.parametrize("node", ["syn://thing/a", NODE, "syn://thing/custom_lab-host",
                                 "syn://thing/" + "a" * 255])
def test_full_reference_round_trip_and_out_of_order(format, node):
    frames = build_frames(node, (format,))
    decoder = BeaconDecoder()
    results = observe_frames(decoder, reversed(frames))
    assert all(result["claimedNodeRef"] is None for result in results[:-1])
    assert results[-1]["claimedNodeRef"] == node
    assert results[-1]["identityEvidence"] == "reassembled-reference"
    assert all(result["authenticated"] is False for result in results)
    assert results[-1]["format"] == format


def test_max_reference_bound_and_alias_normalization():
    node = "syn://thing/" + "a" * 255
    assert len(node) == MAX_NODE_REF_BYTES
    assert len(build_frames(node, ("syn",))) == MAX_NODE_REF_FRAGMENTS == 39
    assert len(build_frames(node, ("syn-ibeacon",))) == MAX_IBEACON_NODE_REF_FRAGMENTS == 31
    assert build_frames("synny://thing/a") == build_frames("syn://thing/a")
    with pytest.raises(ValueError):
        build_frames(node + "a")


@pytest.mark.parametrize("node", ["", "syn://event/a", "syn://thing/UPPER",
    "syn://thing/a\n", "syn://thing/a/b", "syn://thing/a?b", "syn://thing/é",
    "syn://thing/a--b", "syn://thing/a_", "syn://thing/\ud800", None, 1])
def test_invalid_references_rejected_without_truncation(node):
    with pytest.raises(ValueError):
        canonical_node_ref(node)


@pytest.mark.parametrize("format", ["syn", "syn-ibeacon"])
def test_adapter_and_source_are_separate_reassembly_domains(format):
    frames = build_frames(NODE, (format,))
    decoder = BeaconDecoder()
    observe_frames(decoder, frames[:-1], adapter="hci0", source="A")
    assert observe_frames(decoder, frames[-1:], adapter="hci1", source="A")[-1]["claimedNodeRef"] is None
    assert observe_frames(decoder, frames[-1:], adapter="hci0", source="B")[-1]["claimedNodeRef"] is None
    assert observe_frames(decoder, frames[-1:], adapter="hci0", source="A")[-1]["claimedNodeRef"] == NODE


def test_transport_domains_do_not_mix_even_with_the_same_token():
    native = build_frames(NODE, ("syn",))
    fallback = build_frames(NODE, ("syn-ibeacon",))
    assert body(native[0])[2] == body(fallback[0])[2]
    decoder = BeaconDecoder()
    observe_frames(decoder, native[:-1])
    assert observe_frames(decoder, fallback[1:])[-1]["claimedNodeRef"] is None
    assert observe_frames(decoder, native[-1:])[-1]["claimedNodeRef"] == NODE
    assert observe_frames(decoder, fallback[:1])[-1]["claimedNodeRef"] == NODE


@pytest.mark.parametrize("format", ["syn", "syn-ibeacon"])
def test_corruption_and_conflicting_duplicate_never_claim_a_reference(format):
    frames = build_frames(NODE, (format,))
    corrupted = bytearray(body(frames[1]))
    corrupted[4] ^= 1
    replacement = changed_body(frames[1], bytes(corrupted))
    decoder = BeaconDecoder()
    result = observe_frames(decoder, (frames[0], replacement, *frames[2:]))[-1]
    assert result["claimedNodeRef"] is None
    assert result["identityEvidence"] == "invalid-reference-checksum"
    decoder = BeaconDecoder()
    observe_frames(decoder, frames[:2])
    result = observe_frames(decoder, (replacement,))[-1]
    assert result["identityEvidence"] == "conflicting-fragments"
    assert observe_frames(decoder, frames[2:])[-1]["claimedNodeRef"] is None


@pytest.mark.parametrize("format", ["syn", "syn-ibeacon"])
def test_token_collision_with_different_count_is_not_merged(format):
    frames = build_frames(NODE, (format,))
    decoder = BeaconDecoder()
    observe_frames(decoder, frames[:1])
    changed = bytearray(body(frames[1]))
    changed[1] += 1
    result = observe_frames(decoder, (changed_body(frames[1], bytes(changed)),))[-1]
    assert result["identityEvidence"] == "conflicting-fragments"
    assert not result["claimedNodeRef"]


@pytest.mark.parametrize("format", ["syn", "syn-ibeacon"])
def test_partial_assembly_ttl_is_not_extended_by_duplicates(format):
    frames = build_frames(NODE, (format,))
    decoder = BeaconDecoder(ttl_seconds=10)
    observe_frames(decoder, frames[:1], now=0)
    observe_frames(decoder, frames[:1], now=9)
    assert observe_frames(decoder, frames[1:], now=10)[-1]["claimedNodeRef"] is None
    assert observe_frames(decoder, frames[:1], now=11)[-1]["claimedNodeRef"] == NODE


def test_bounded_assembly_and_identity_caches_and_clock_reset():
    frames = build_frames(NODE, ("syn",))
    decoder = BeaconDecoder(max_assemblies=2)
    for source in ("A", "B", "C"):
        observe_frames(decoder, frames[:1], source=source, now=0)
    assert len(decoder._assemblies) == 2
    assert observe_frames(decoder, frames[1:], source="A", now=1)[-1]["claimedNodeRef"] is None
    for source in ("A", "B", "C"):
        observe_frames(decoder, frames, source=source, now=2)
    assert len(decoder._identities) == 2
    decoder.observe(received(frames[0]), now_monotonic=1)
    assert len(decoder._identities) == 0
    assert len(decoder._assemblies) == 1


def test_forget_adapter_clears_all_its_transports_and_identity_only():
    decoder = BeaconDecoder()
    complete = build_frames(NODE, ("syn",))
    partial = build_frames("syn://thing/other", ("syn", "syn-ibeacon"))
    for adapter in ("hci0", "hci1"):
        observe_frames(decoder, complete, adapter=adapter)
        observe_frames(decoder, (partial[0], partial[-1]), adapter=adapter)
    decoder.forget_adapter("hci0")
    assert decoder._assemblies and all(key[0] == "hci1" for key in decoder._assemblies)
    assert decoder._identities and all(key[0] == "hci1" for key in decoder._identities)
    hint = build_frames(NODE, ("ibeacon",))[0]
    assert observe_frames(decoder, (hint,), adapter="hci0")[-1]["claimedNodeRef"] is None
    assert observe_frames(decoder, (hint,), adapter="hci1")[-1]["claimedNodeRef"] == NODE


@pytest.mark.parametrize("format", ["ibeacon", "eddystone-uid", "altbeacon"])
def test_hints_require_a_matching_unexpired_reference_on_the_same_source(format):
    frame = build_frames(NODE, (format,), company_id=0x1234)[0]
    decoder = BeaconDecoder(ttl_seconds=10)
    result = observe_frames(decoder, (frame,))[-1]
    assert result["isSyn"] and not result["claimedNodeRef"]
    assert result["identityEvidence"] == "short-hint-only"
    observe_frames(decoder, build_frames(NODE, ("syn",)))
    result = observe_frames(decoder, (frame,), now=1)[-1]
    assert result["claimedNodeRef"] == NODE
    assert not result["authenticated"]
    assert observe_frames(decoder, (frame,), source="other", now=1)[-1]["claimedNodeRef"] is None
    other = build_frames("syn://thing/different", (format,), company_id=0x1234)[0]
    assert observe_frames(decoder, (other,), now=1)[-1]["claimedNodeRef"] is None
    assert observe_frames(decoder, (frame,), now=10)[-1]["claimedNodeRef"] is None


def test_conflicting_identity_hints_in_one_report_do_not_resolve():
    frames = build_frames(NODE, ("syn",))
    mismatch = build_frames("syn://thing/other", ("ibeacon",))[0]
    decoder = BeaconDecoder()
    observe_frames(decoder, frames[:-1])
    sample = replace(received(frames[-1]), manufacturer_data=mismatch.manufacturer_data)
    result = decoder.observe(sample, now_monotonic=0)
    assert result["claimedNodeRef"] is None
    assert result["identityEvidence"] == "conflicting-identity-hints"
    assert not decoder._identities


def test_foreign_named_and_unrelated_beacons_stay_unidentified():
    decoder = BeaconDecoder()
    assert not decoder.observe(ReceivedAdvertisement("hci0", "peer", name="Syn node"))["isSyn"]
    foreign = ReceivedAdvertisement("hci0", "peer", manufacturer_data={IBEACON_COMPANY_ID:
        b"\x02\x15" + b"x" * 16 + b"abcd" + b"\xc5"})
    result = decoder.observe(foreign)
    assert result["format"] == "ibeacon" and not result["isSyn"]
    for kind, expected in ((0x00, "eddystone-uid"), (0x10, "eddystone-url"),
                           (0x20, "eddystone-tlm"), (0x30, "eddystone-eid")):
        sample = ReceivedAdvertisement("hci0", "peer", service_data={
            EDDYSTONE_SERVICE_UUID: bytes((kind,)) + b"x" * 19})
        result = decoder.observe(sample)
        assert result["format"] == expected and not result["isSyn"]


@pytest.mark.parametrize("data", [b"", b"\x40", b"\x40\xff\x00abc", b"\xc0\x03\x00abc",
                                    b"\x7f\x03\x00abcdefg", b"\x40\x03\x00abc"])
def test_malformed_fragments_do_not_raise_or_claim_identity(data):
    sample = ReceivedAdvertisement("hci0", "peer", service_data={SYN_SERVICE_UUID: data})
    result = BeaconDecoder().observe(sample)
    assert result["isSyn"] and result["claimedNodeRef"] is None
    assert result["identityEvidence"] == "malformed-fragment"


def test_fixed_length_fallback_rejects_nonzero_padding():
    frames = list(build_frames("syn://thing/a", ("syn-ibeacon",)))
    last = bytearray(body(frames[-1]))
    last[-1] = 1
    frames[-1] = changed_body(frames[-1], bytes(last))
    result = observe_frames(BeaconDecoder(), frames)[-1]
    assert result["claimedNodeRef"] is None
    assert result["identityEvidence"] == "invalid-reference-length"


def test_valid_checksum_does_not_make_an_invalid_reference_valid():
    reference = b"syn://thing/INVALID"
    envelope = len(reference).to_bytes(2, "big") + reference
    payload = envelope + zlib.crc32(b"SYN-BEACON/1\0" + envelope).to_bytes(4, "big")
    count = (len(payload) + FRAGMENT_CHUNK_BYTES - 1) // FRAGMENT_CHUNK_BYTES
    token = hashlib.sha256(payload).digest()[0]
    decoder = BeaconDecoder()
    for index in range(count):
        data = bytes((0x40 | index, count, token)) + payload[index * 7:(index + 1) * 7]
        result = decoder.observe(ReceivedAdvertisement("hci0", "peer",
            service_data={SYN_SERVICE_UUID: data}), now_monotonic=0)
    assert result["identityEvidence"] == "invalid-reference"
    assert result["claimedNodeRef"] is None


@pytest.mark.parametrize("formats", [("eddystone-url",), ("eddystone-eid",),
                                     ("eddystone-tlm",), ("unbranded",)])
def test_unimplemented_formats_cannot_emit_unbranded_packets(formats):
    with pytest.raises(UnsupportedBeaconFormat):
        build_frames(NODE, formats)


def test_missing_or_invalid_company_id_and_duplicate_formats():
    with pytest.raises(UnsupportedBeaconFormat):
        build_frames(NODE, ("altbeacon",))
    for company in (True, -1, 65536, "1234"):
        with pytest.raises(ValueError):
            build_frames(NODE, ("altbeacon",), company_id=company)
    with pytest.raises(ValueError):
        build_frames(NODE, ("syn", "syn"))
    with pytest.raises(ValueError):
        build_frames(NODE, "syn")
    assert build_frames(NODE, ()) == ()


def test_uuid_wire_order_and_ad_parser():
    uuid = "00112233-4455-6677-8899-aabbccddeeff"
    assert uuid_wire_bytes(uuid).hex() == "ffeeddccbbaa99887766554433221100"
    assert canonical_uuid("FEAA") == EDDYSTONE_SERVICE_UUID
    assert canonical_uuid("12345678") == "12345678-0000-1000-8000-00805f9b34fb"
    encoded = serialize_advertising_data(service_uuids=(uuid, "feaa", "12345678"),
                                        local_name="Syn", include_flags=False)
    parsed = parse_advertising_data(encoded + b"\0\0")
    assert (7, uuid_wire_bytes(uuid)) in parsed
    assert (5, b"\x78\x56\x34\x12") in parsed
    assert (9, b"Syn") in parsed
    for invalid in (b"\x02\x01", b"\0\x01", b"\x04\x01\x02", b"x" * 1651):
        with pytest.raises(ValueError):
            parse_advertising_data(invalid)
    with pytest.raises(ValueError):
        serialize_advertising_data(service_data={SYN_SERVICE_UUID: b"x" * 239})
    for invalid in ("{00112233-4455-6677-8899-aabbccddeeff}", " FEAA", "000", None):
        with pytest.raises(ValueError):
            canonical_uuid(invalid)


def test_models_detach_native_containers_and_emit_bounded_json():
    data = {"FEAA": bytearray(b"abc")}
    details = {"backend": "test", "values": [1, True, None]}
    instant = datetime(2026, 1, 1, 1, tzinfo=timezone(timedelta(hours=1)))
    sample = ReceivedAdvertisement("hci0", "peer", service_data=data,
        manufacturer_data={76: b"\0\1"}, observed_at=instant, details=details)
    data["FEAA"][0] = 0
    details["values"].append(2)
    document = sample.to_dict()
    assert document["serviceData"] == {EDDYSTONE_SERVICE_UUID: "616263"}
    assert document["manufacturerData"] == {"76": "0001"}
    assert document["details"]["values"] == [1, True, None]
    assert document["observedAt"] == "2026-01-01T00:00:00+00:00"
    json.dumps(document, allow_nan=False)
    adapter = Adapter("a", "test", "a", "Adapter", "epoch", True, True)
    assert adapter.to_dict()["maxAdvertisingDataBytes"] == 31


@pytest.mark.parametrize("kwargs", [
    {"service_data": {SYN_SERVICE_UUID: b"x" * 1651}},
    {"service_data": {"bad uuid": b"x"}},
    {"service_data": {"FEAA": b"x", EDDYSTONE_SERVICE_UUID: b"x"}},
    {"manufacturer_data": {True: b"x"}},
    {"manufacturer_data": {value: b"x" * 1650 for value in range(6)}},
    {"service_uuids": ("feaa",) * 33},
    {"observed_at": datetime(2026, 1, 1)},
    {"rssi": True}, {"rssi": -128}, {"name": "\ud800"},
    {"name": "Syn\0node"}, {"details": {"bad": 1 << 53}},
    {"details": {"bad": 1e100}},
    {"details": {"bad": float("nan")}}, {"details": {"bad": b"raw"}},
    {"details": {"bad": "x" * 8193}}, {"details": []},
])
def test_invalid_native_values_are_rejected(kwargs):
    with pytest.raises(ValueError):
        ReceivedAdvertisement("hci0", "peer", **kwargs)


def test_defensive_decoder_and_json_bounds():
    decoder = BeaconDecoder()
    assert decoder.observe(object())["claimedNodeRef"] is None
    sample = ReceivedAdvertisement("hci0", "peer")
    assert decoder.observe(sample, now_monotonic=float("nan"))["claimedNodeRef"] is None
    sample.service_data["bad"] = object()
    assert decoder.observe(sample)["identityEvidence"] == "malformed-advertisement"
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError):
        bounded_json(cycle)
    for ttl in (0, -1, 3601, float("inf"), True):
        with pytest.raises(ValueError):
            BeaconDecoder(ttl_seconds=ttl)
    for maximum in (0, -1, 4097, True):
        with pytest.raises(ValueError):
            BeaconDecoder(max_assemblies=maximum)
