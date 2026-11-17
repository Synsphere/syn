"""Terminology-only correction and append-only upgrades of the previous type."""
from dataclasses import replace
from datetime import timedelta
import hashlib
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from syn.positions import schema as db
from syn.positions.config import PositionConfig
from syn.positions.store import workspace_for
from syn.positions.types import (Estimate, GEOGRAPHIC, GEOGRAPHIC_SPACE,
                                 GPS_READING, LEGACY_GEOGRAPHIC, Reading,
                                 Space, WGS84, geographic)

NODE = "syn://thing/geodetic-rename-test"


def seed_legacy_history(state, now):
    """Fixture for old hashed objects after the frame metadata migration.

    Direct writes are deliberately confined to this fixture: production writers
    never create a legacy Estimate. The actual _record/_update ledger path is used.
    """
    store, sessions, _ = state
    value = replace(geographic(51.5, -0.1, 25, 4, now, "gps",
                               altitudeDatum="mean-sea-level",
                               provenance={"receiver": "fixture"}),
                    typeRef=LEGACY_GEOGRAPHIC)
    with sessions.begin() as session:
        store.register_space(WGS84, session=session)
        session.execute(db.primitive["things"].insert().values(
            id=NODE, name="Legacy node", typeRef="syn://type/device", knowledge={}))
        session.execute(db.nodes.insert().values(
            nodeRef=NODE, ledgerWorkspace=workspace_for(NODE), createdAt=now, probeReport=[]))
        reading = Reading("gps", now, now, value, "3d")
        event = store._record(session, NODE, "event", GPS_READING,
                              {**reading.to_dict(), "nodeRef": NODE}, sampleKey=reading.sampleKey)
        store._update(session, NODE, value, None, event)
    # Keep a later stationary confirmation so a rename cannot reset freshness.
    later = now + timedelta(seconds=5)
    confirmation = Reading("gps", later, later, replace(value, observedAt=later), "3d")
    with sessions.begin() as session:
        event = store._record(session, NODE, "event", GPS_READING,
                              {**confirmation.to_dict(), "nodeRef": NODE},
                              sampleKey=confirmation.sampleKey)
        row = store._current(session, NODE, WGS84.reference)
        session.execute(db.positions.update().where(db.positions.c.nodeRef == NODE)
                        .values(lastObservedAt=later))
        store._ledger(session, NODE, {
            "profile": "syn.position-confirmation/1", "spaceRef": WGS84.reference,
            "emberRef": row["emberRef"], "eventRef": event,
            "lastObservedAt": later.isoformat(),
        })
    return store.current(NODE)[0]


def snapshots(state):
    _, sessions, ledger = state
    with sessions() as session:
        return {
            "records": [dict(v) for v in session.execute(select(db.records)
                         .order_by(db.records.c.receiptSequence)).mappings()],
            "messages": [dict(v) for v in session.execute(select(ledger.messages)
                          .order_by(ledger.messages.c.sequence)).mappings()],
            "embers": {v["id"]: dict(v) for v in session.execute(select(db.primitive["embers"])).mappings()},
            "events": {v["id"]: dict(v) for v in session.execute(select(db.primitive["events"])).mappings()},
        }


def test_canonical_name_and_inheritance_contract(state, now):
    assert GEOGRAPHIC == "syn://type/geographic-geodetic"
    assert WGS84.typeRef == GEOGRAPHIC
    assert WGS84.reference == "syn://thing/space-wgs84-geodetic"
    assert WGS84.axes == ("latitude", "longitude", "altitude")
    assert WGS84.units == ("degree", "degree", "metre")
    _, sessions, _ = state
    with sessions() as session:
        for child in (GPS_READING, "syn://type/os-location-reading"):
            parents = set(session.scalars(select(db.typeParents.c.parentRef)
                                          .where(db.typeParents.c.childRef == child)))
            assert parents == {GEOGRAPHIC, "syn://type/event"}
        assert session.scalar(select(db.typeParents.c.parentRef)
                              .where(db.typeParents.c.childRef == LEGACY_GEOGRAPHIC)) == GEOGRAPHIC
        legacy = session.scalar(select(db.primitive["types"].c.knowledge)
                                .where(db.primitive["types"].c.id == LEGACY_GEOGRAPHIC))
        assert legacy["deprecated"] is True
        assert legacy["replacedBy"] == GEOGRAPHIC
        props = set(session.scalars(select(db.typeProperties.c.name)
                                    .where(db.typeProperties.c.type == GEOGRAPHIC)))
        assert {"latitude", "longitude", "altitude", "errorMeters", "altitudeDatum", "verticalError"} <= props


def test_name_only_not_coordinate_conversion_and_same_gps_sample(now):
    current = geographic(51.5, -0.1, 25, 4, now, "gps", altitudeDatum="ellipsoid")
    legacy = replace(current, typeRef=LEGACY_GEOGRAPHIC)
    before, after = legacy.to_dict(), current.to_dict()
    assert before.pop("typeRef") == LEGACY_GEOGRAPHIC
    assert after.pop("typeRef") == GEOGRAPHIC
    assert before == after
    assert Estimate.from_dict(legacy.to_dict()) == legacy  # Audit reads keep the signed identifier.
    assert Reading("gps", now, now, legacy, "3d").sampleKey == Reading("gps", now, now, current, "3d").sampleKey
    with pytest.raises(ValueError, match="legacy"):
        Space(GEOGRAPHIC_SPACE, LEGACY_GEOGRAPHIC, WGS84.axes, WGS84.units, "metre")


def test_upgrade_preserves_hashed_history_epoch_freshness_and_event(state, now):
    store, _, _ = state
    old = seed_legacy_history(state, now)
    verified = store.verify(NODE)
    before = snapshots(state)
    reference = store.upgrade_geographic_type(NODE)
    current = store.current(NODE)[0]
    assert current["emberRef"] == reference != old["emberRef"]
    assert current["revision"] == old["revision"] + 1
    assert current["lastObservedAt"] == old["lastObservedAt"]
    assert current["estimate"]["typeRef"] == GEOGRAPHIC
    for key in ("coordinates", "error", "errorUnit", "observedAt", "source",
                "uncertaintyMethod", "confidenceLevel", "verticalError", "altitudeDatum"):
        assert current["estimate"][key] == old["estimate"][key]
    assert current["estimate"]["provenance"]["receiver"] == "fixture"
    assert current["estimate"]["provenance"]["typeRename"]["previousEmberRef"] == old["emberRef"]
    after = snapshots(state)
    assert after["records"][:-1] == before["records"]
    assert after["messages"][:-2] == before["messages"]
    assert after["events"] == before["events"]
    for ref, body in before["embers"].items():
        assert after["embers"][ref] == body
    assert after["embers"][reference]["knowledge"]["observationEventRef"] == before["embers"][old["emberRef"]]["knowledge"]["observationEventRef"]
    assert store.verify(NODE)["objects"] == verified["objects"] + 1


def test_upgrade_is_idempotent(state, now):
    store, _, _ = state
    seed_legacy_history(state, now)
    assert store.upgrade_geographic_type(NODE)
    before = store.verify(NODE), store.current(NODE), snapshots(state)
    assert store.upgrade_geographic_type(NODE) is None
    assert (store.verify(NODE), store.current(NODE), snapshots(state)) == before


def test_upgrade_ledger_failure_rolls_back(state, now, monkeypatch):
    store, _, _ = state
    seed_legacy_history(state, now)
    before = store.verify(NODE), store.current(NODE), snapshots(state)
    append = store.ledger.appendInTransaction
    def fail(session, workspace, body):
        if body["payload"]["profile"] == "syn.position-canonical/1":
            raise RuntimeError("injected rename ledger failure")
        return append(session, workspace, body)
    monkeypatch.setattr(store.ledger, "appendInTransaction", fail)
    with pytest.raises(RuntimeError, match="rename ledger"):
        store.upgrade_geographic_type(NODE)
    assert (store.verify(NODE), store.current(NODE), snapshots(state)) == before


def test_runtime_upgrades_before_polling_without_timezone_reset(state, now, monkeypatch, tmp_path):
    from syn.positions import runtime
    store, sessions, _ = state
    seed_legacy_history(state, now)
    monkeypatch.setattr(runtime, "bootstrap", Mock(side_effect=AssertionError("Reset timezone prior")))
    node = runtime.PositionRuntime(sessions, "unused", PositionConfig(nodeRef=NODE, stateDirectory=str(tmp_path)), identity="test")
    scheduler = Mock(schedulerKey="rename-node")
    node.prepare(scheduler)
    assert store.current(NODE)[0]["estimate"]["typeRef"] == GEOGRAPHIC
    scheduler.every.assert_called_once()
    before = store.verify(NODE)
    node.prepare(scheduler)
    assert store.verify(NODE) == before


def test_bootstrap_and_new_fix_handle_legacy_current_without_changing_other_spaces(state, now):
    store, _, _ = state
    old = seed_legacy_history(state, now)
    other = Space("syn://thing/rename-local-space", "syn://type/rename-local-position",
                  ("x", "y", "z"), ("metre",) * 3, "metre", "euclidean")
    store.register_space(other)
    store.put(NODE, Estimate(other.reference, other.typeRef, {"x": 1, "y": 2, "z": 3},
                            .1, "metre", now, "robot"), now=now)
    local = next(v for v in store.current(NODE) if v["spaceRef"] == other.reference)
    store.bootstrap(NODE, geographic(0, 0, None, 100000, now, "timezone"))
    updated = next(v for v in store.current(NODE) if v["spaceRef"] == WGS84.reference)
    assert updated["estimate"]["coordinates"] == old["estimate"]["coordinates"]
    assert next(v for v in store.current(NODE) if v["spaceRef"] == other.reference) == local
    later = now + timedelta(seconds=6)
    reading = Reading("gps", later, later, geographic(51.6, -.1, 25, 4, later, "gps", altitudeDatum="mean-sea-level"), "3d")
    assert store.observe(NODE, reading)["decision"] == "replace"
    assert next(v for v in store.current(NODE) if v["spaceRef"] == other.reference) == local
    store.verify(NODE)


def test_direct_reconciliation_promotes_old_type_before_comparison(state, now):
    store, _, _ = state
    seed_legacy_history(state, now)
    later = now + timedelta(seconds=6)
    reading = Reading("gps", later, later, geographic(51.5, -.1, 25, 4, later, "gps", altitudeDatum="mean-sea-level"), "3d")
    assert store.observe(NODE, reading)["decision"] == "unchanged"
    assert store.current(NODE)[0]["estimate"]["typeRef"] == GEOGRAPHIC
    assert store.current(NODE)[0]["lastObservedAt"] == later.isoformat()
    store.verify(NODE)


def test_frozen_initial_migration_and_additive_sql_contract(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    directory = root / "src/syn/alembic/versions"
    original = directory / "20261117_0016_position_awareness.py"
    assert hashlib.sha256(original.read_bytes()).hexdigest() == "52b1dc3314c9f2f118ea77b8063f4e31a205413189114abb8325eb08fb8289ee"
    path = directory / "20261117_0017_geographic_geodetic.py"
    spec = importlib.util.spec_from_file_location("geodetic_rename_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "20261117_0016"
    assert module.revision == "20261117_0017"
    emitted = []
    monkeypatch.setattr(module.op, "execute", emitted.append)
    module.upgrade()
    assert emitted == list(module.STATEMENTS)
    sql = "\n".join(emitted)
    assert 'LOCK TABLE "coordinateSpaces" IN ACCESS EXCLUSIVE MODE' in sql
    assert "DROP TRIGGER coordinate_spaces_immutable" in sql
    assert "CREATE TRIGGER coordinate_spaces_immutable" in sql
    assert sql.index("DROP TRIGGER coordinate_spaces_immutable") < sql.index('UPDATE "coordinateSpaces"') < sql.index("CREATE TRIGGER coordinate_spaces_immutable")
    assert GEOGRAPHIC in sql
    for table in ("embers", "events", '"positionRecords"', '"nodePositions"', "syn_communication_messages"):
        assert f"UPDATE {table}" not in sql
        assert f"DELETE FROM {table}" not in sql
    with pytest.raises(RuntimeError, match="append-only"):
        module.downgrade()
