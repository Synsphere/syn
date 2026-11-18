"""Durable observations, bounded radio input, honest identity and atomicity."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from syn.bluetooth import schema as db
from syn.bluetooth.store import (
    BluetoothStore, BluetoothStoreConflict, BluetoothStoreError,
    PROFILE, digest, event_workspace, validate_payload,
)
from syn.communication import heads, messages
from syn.positions.store import workspace_for
from syn_client.protocol import GENESIS_HASH, canonical, verify_receipt


NOW = datetime(2026, 11, 18, 12, 30, tzinfo=UTC)
PEER = "syn://thing/another-syn-node"


def receipt_chain(sessions, workspace):
    with sessions() as session:
        rows = session.execute(select(messages).where(messages.c.workspace_id == workspace)
            .order_by(messages.c.sequence)).mappings().all()
        head = session.execute(select(heads).where(heads.c.workspace_id == workspace)).mappings().one()
    previous = GENESIS_HASH
    for number, row in enumerate(rows, 1):
        receipt = {"schema_version": 1, **dict(row)}
        verify_receipt(receipt, workspace)
        assert receipt["sequence"] == number
        assert receipt["previous_hash"] == previous
        previous = receipt["hash"]
    assert head["sequence"] == len(rows) and head["hash"] == previous
    return rows


def counts(sessions):
    with sessions() as session:
        return tuple(session.execute(select(func.count()).select_from(table)).scalar_one()
            for table in (db.primitive["events"], db.primitive["embers"],
                          db.eventTargets, db.chronicle, messages, heads))


def insert_peer(sessions, reference=PEER):
    with sessions.begin() as session:
        session.execute(db.primitive["things"].insert().values(
            id=reference, name="Known Syn peer", typeRef="syn://type/device",
            knowledge={"trusted-local-metadata": "must remain unchanged"}))


def test_prepare_seeds_native_types_and_one_beacon_definition_receipt(bluetooth_state):
    store, node, sessions = bluetooth_state
    before = counts(sessions)
    first = store.prepare(node)
    second = BluetoothStore(sessions).prepare(node)
    assert first == second
    assert counts(sessions) == before
    with sessions() as session:
        beacon = session.execute(select(db.primitive["things"]).where(
            db.primitive["things"].c.id == first["beaconRef"])).mappings().one()
        assert beacon["typeRef"] == db.BEACON_TYPE
        assert beacon["knowledge"]["nodeRef"] == node
        parents = set(session.execute(select(db.typeParents.c.childRef, db.typeParents.c.parentRef)))
        assert (db.RECEPTION_TYPE, "syn://type/event") in parents
        assert (db.OBSERVATION_TYPE, "syn://type/observation-result") in parents
    chain = receipt_chain(sessions, workspace_for(node))
    assert len(chain) == 1
    assert chain[0]["payload"]["profile"] == "syn.bluetooth-beacon-registration/1"
    assert chain[0]["payload"]["contentHash"] == digest(chain[0]["payload"]["snapshot"])


@pytest.mark.parametrize("kind", ["transmission-requested", "transmission-started",
    "transmission-stopped", "transmission-failed"])
def test_transmission_native_snapshots_and_same_observation_in_both_ledgers(bluetooth_state, kind):
    store, node, sessions = bluetooth_state
    payload = {"adapter": {"key": "bluez:/org/bluez/hci0", "connectionEpoch": "epoch-1"},
        "frame": {"format": "syn", "serviceData": "53594e0100"},
        "evidenceKind": "backend-callback"}
    result = store.record(node, kind, payload, observed_at=NOW, operation_id="cycle-1:" + kind)
    assert not result["duplicate"]
    payload["frame"]["serviceData"] = "mutated-after-return"
    with sessions() as session:
        event = session.execute(select(db.primitive["events"])).mappings().one()
        ember = session.execute(select(db.primitive["embers"])).mappings().one()
        assert event["id"] == result["eventRef"] and event["typeRef"] == db.TRANSMISSION_TYPE
        assert ember["id"] == result["emberRef"] and ember["typeRef"] == db.OBSERVATION_TYPE
        assert ember["knowledge"]["payload"]["frame"]["serviceData"] == "53594e0100"
        assert ember["knowledge"]["airReceptionVerified"] is False
        targets = {row.targetRef: row.role for row in session.execute(select(db.eventTargets))}
        assert targets == {node: "observing-node", result["emberRef"]: "observation-result",
            store.beacon_ref(node): "local-beacon"}
        links = session.execute(select(db.chronicle)).mappings().all()
        assert {row["objectRef"] for row in links} == {node, result["eventRef"]}
        assert {row["details"]["emberRef"] for row in links} == {result["emberRef"]}
    own = receipt_chain(sessions, workspace_for(node))[-1]
    event_receipt = receipt_chain(sessions, event_workspace(result["eventRef"]))[0]
    assert own["payload"] == event_receipt["payload"]
    assert own["message_id"] == event_receipt["message_id"]
    assert own["hash"] != event_receipt["hash"]
    assert own["payload"]["contentHash"] == digest(own["payload"]["snapshot"])
    assert own["payload"]["eventContentHash"] == digest(own["payload"]["eventSnapshot"])


def test_generic_reception_preserves_aggregation_rssi_and_foreign_beacon_data(bluetooth_state):
    store, node, sessions = bluetooth_state
    report = {"adapter": "hci0", "address": "12:34:56:78:9A:BC", "count": 7,
        "firstObservedAt": NOW.isoformat(), "lastObservedAt": (NOW + timedelta(seconds=3)).isoformat(),
        "firstRssi": -74, "lastRssi": -67, "minRssi": -78, "maxRssi": -66,
        "manufacturerData": {"76": "0215abcdef"}, "name": "Not a Syn beacon"}
    result = store.record(node, "reception", {"report": report}, observed_at=NOW)
    assert result["identityStatus"] == "unidentified" and result["peerRef"] is None
    assert result["receipt"]["payload"]["snapshot"]["knowledge"]["payload"]["report"] == report
    assert result["receipt"]["payload"]["eventSnapshot"]["typeRef"] == db.RECEPTION_TYPE
    with sessions() as session:
        assert session.execute(select(func.count()).select_from(db.eventTargets)).scalar_one() == 2
    lost = store.record(node, "loss", {"reason": "queue-overflow", "dropped": 13}, observed_at=NOW)
    assert lost["receipt"]["payload"]["snapshot"]["knowledge"]["payload"]["dropped"] == 13
    receipt_chain(sessions, workspace_for(node))


def test_known_claim_links_existing_thing_without_claiming_authentication_or_mutating_it(bluetooth_state):
    store, node, sessions = bluetooth_state
    insert_peer(sessions)
    with sessions() as session:
        original = dict(session.execute(select(db.primitive["things"]).where(
            db.primitive["things"].c.id == PEER)).mappings().one())
    result = store.record(node, "reception", {"identity": {
        "isSyn": True, "claimedNodeRef": PEER, "verified": True}}, observed_at=NOW)
    assert result["peerRef"] == PEER
    assert result["identityStatus"] == "claimed-known"
    assert result["identity"]["verified"] is False
    assert result["identity"]["authentication"] == "unverified"
    with sessions() as session:
        peer = dict(session.execute(select(db.primitive["things"]).where(
            db.primitive["things"].c.id == PEER)).mappings().one())
        target = session.execute(select(db.eventTargets).where(
            db.eventTargets.c.targetRef == PEER)).mappings().one()
        assert peer == original and target["role"] == "advertised-peer"


def test_unknown_claim_stays_in_knowledge_and_replay_does_not_reinterpret_new_knowledge(bluetooth_state):
    store, node, sessions = bluetooth_state
    payload = {"identity": {"isSyn": True, "claimedNodeRef": PEER}}
    result = store.record(node, "reception", payload, observed_at=NOW, operation_id="unknown-peer")
    assert result["identityStatus"] == "claimed-unknown" and result["peerRef"] is None
    assert result["identity"]["claimedNodeRef"] == PEER
    with sessions() as session:
        assert session.execute(select(db.primitive["things"].c.id).where(
            db.primitive["things"].c.id == PEER)).first() is None
        assert session.execute(select(db.eventTargets).where(db.eventTargets.c.targetRef == PEER)).first() is None
    insert_peer(sessions)
    replay = BluetoothStore(sessions).record(node, "reception", payload, operation_id="unknown-peer")
    assert replay["duplicate"] and replay["identityStatus"] == "claimed-unknown"
    assert replay["receipt"] == result["receipt"]
    assert store.resolve_peer(node, PEER)["identityStatus"] == "claimed-known"


def test_short_hint_and_invalid_claim_never_link_peer(bluetooth_state):
    store, node, sessions = bluetooth_state
    insert_peer(sessions)
    hint = digest(PEER)[:12]
    result = store.record(node, "reception", {"identity": {"isSyn": True,
        "identityHint": hint}}, observed_at=NOW)
    assert result["identityStatus"] == "hint-only" and result["peerRef"] is None
    malformed = store.record(node, "reception", {"identity": {"isSyn": True,
        "claimedNodeRef": "https://attacker.invalid/thing"}}, observed_at=NOW)
    assert malformed["identityStatus"] == "invalid-claim" and malformed["peerRef"] is None
    self_claim = store.record(node, "reception", {"claimedNodeRef": node}, observed_at=NOW)
    assert self_claim["identityStatus"] == "claimed-self"
    with sessions() as session:
        assert session.execute(select(db.eventTargets).where(db.eventTargets.c.targetRef == PEER)).first() is None


def test_incoming_claim_uses_native_thing_grammar_including_underscores(bluetooth_state):
    store, node, sessions = bluetooth_state
    peer = "syn://thing/known_syn_node"
    insert_peer(sessions, peer)
    result = store.record(node, "reception", {"identity": {"isSyn": True,
        "claimedNodeRef": peer}}, observed_at=NOW)
    assert result["identityStatus"] == "claimed-known" and result["peerRef"] == peer
    assert store.resolve_peer(node, "syn://thing/invalid--slug")["identityStatus"] == "invalid-claim"


def test_restart_replay_is_identical_and_conflicting_operation_ids_are_rejected(bluetooth_state):
    store, node, sessions = bluetooth_state
    result = store.record(node, "reception", {"count": 1}, operation_id="retry-me")
    before = counts(sessions)
    replay = BluetoothStore(sessions).record(node, "reception", {"count": 1}, operation_id="retry-me")
    assert replay == {**result, "duplicate": True}
    for kind, payload, observed in [("reception", {"count": 2}, None),
            ("loss", {"count": 1}, None), ("reception", {"count": 1}, NOW)]:
        with pytest.raises(BluetoothStoreConflict, match="different contents"):
            store.record(node, kind, payload, operation_id="retry-me", observed_at=observed)
    assert counts(sessions) == before


def test_second_ledger_failure_rolls_back_every_object_target_chronicle_and_receipt(bluetooth_state, monkeypatch):
    store, node, sessions = bluetooth_state
    before, head = counts(sessions), store.ledger.head(workspace_for(node))
    append = store.ledger.appendInTransaction

    def fail(session, workspace, message):
        if workspace != workspace_for(node):
            raise RuntimeError("Event ledger unavailable")
        return append(session, workspace, message)

    monkeypatch.setattr(store.ledger, "appendInTransaction", fail)
    with pytest.raises(RuntimeError, match="Event ledger unavailable"):
        store.record(node, "transmission-started", {}, observed_at=NOW, operation_id="failed-commit")
    assert counts(sessions) == before
    assert store.ledger.head(workspace_for(node)) == head
    monkeypatch.setattr(store.ledger, "appendInTransaction", append)
    assert not store.record(node, "transmission-started", {}, observed_at=NOW,
        operation_id="failed-commit")["duplicate"]


def test_fabricated_event_receipt_cannot_commit_unledgered_native_objects(bluetooth_state, monkeypatch):
    store, node, sessions = bluetooth_state
    before = counts(sessions)
    append = store.ledger.appendInTransaction

    def fabricate(session, workspace, message):
        if workspace != workspace_for(node):
            return {"workspace_id": workspace, "sequence": 1, "hash": "0" * 64}
        return append(session, workspace, message)

    monkeypatch.setattr(store.ledger, "appendInTransaction", fabricate)
    with pytest.raises(BluetoothStoreConflict, match="missing a matching ledger"):
        store.record(node, "reception", {}, observed_at=NOW)
    assert counts(sessions) == before


def test_native_snapshot_changes_are_detected_on_retry(bluetooth_state):
    store, node, sessions = bluetooth_state
    result = store.record(node, "reception", {}, observed_at=NOW, operation_id="immutable")
    with sessions.begin() as session:
        session.execute(db.primitive["events"].update().where(
            db.primitive["events"].c.id == result["eventRef"]).values(name="Tampered"))
    with pytest.raises(BluetoothStoreConflict, match="retained ledger snapshots"):
        store.record(node, "reception", {}, observed_at=NOW, operation_id="immutable")


@pytest.mark.parametrize("payload", [[], {"nan": float("nan")}, {"bytes": b"raw"},
    {"timestamp": NOW}, {"count": 2**63}, {"long": "x" * 16385},
    {"array": list(range(257))}, {1: "bad-key"},
    {"unicode": "\U0001f600" * 8192}, {"name": "nul\x00inside"},
    {"nul\x00key": 1}, {"name": "\ud800"}])
def test_non_json_or_unbounded_radio_inputs_never_write(bluetooth_state, payload):
    store, node, sessions = bluetooth_state
    before = counts(sessions)
    with pytest.raises(BluetoothStoreError):
        validate_payload(payload)
    with pytest.raises(BluetoothStoreError):
        store.record(node, "reception", payload, observed_at=NOW)
    assert counts(sessions) == before


def test_bounded_generic_payload_and_integral_float_hashes(bluetooth_state):
    store, node, sessions = bluetooth_state
    payload = {"rssi": -70.0, **{f"part{i}": "a" * 10000 for i in range(4)}}
    result = store.record(node, "reception", payload, observed_at=NOW, operation_id="bounded")
    retained = result["receipt"]["payload"]
    assert retained["profile"] == PROFILE and len(canonical(retained)) <= 65_536
    assert type(retained["snapshot"]["knowledge"]["payload"]["rssi"]) is int
    assert BluetoothStore(sessions).record(node, "reception", payload, operation_id="bounded")["duplicate"]


def test_unprepared_node_cannot_record_or_prepare_another_nodes_ledger(bluetooth_state):
    store, _, sessions = bluetooth_state
    before = counts(sessions)
    with pytest.raises(BluetoothStoreConflict, match="prepared Syn node"):
        store.prepare("syn://thing/unprepared")
    with pytest.raises(BluetoothStoreConflict, match="prepared Syn node"):
        store.record("syn://thing/unprepared", "reception", {})
    assert counts(sessions) == before
