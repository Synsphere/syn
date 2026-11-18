"""Opt-in real PostgreSQL native triggers, replay and transaction qualification.

SYN_BLUETOOTH_TEST_DATABASE_URL must name a disposable database with DDL rights.
Each run creates and removes only its own random schema.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from syn.bluetooth import schema as db
from syn.bluetooth.store import BluetoothStore, event_workspace
from syn.communication import messages
from syn.positions.store import PositionStore, workspace_for
from syn.positions.types import geographic
from syn_client.protocol import verify_receipt


URL = os.environ.get("SYN_BLUETOOTH_TEST_DATABASE_URL")
pytestmark = [pytest.mark.database, pytest.mark.skipif(not URL,
    reason="Real Bluetooth PostgreSQL opt-in URL is not configured")]


@pytest.fixture(scope="module")
def postgres():
    from syn.database import createEngine
    name = "syn_bluetooth_test_" + uuid4().hex
    admin, engine = create_engine(URL), None
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{name}"'))
            connection.execute(text(f'CREATE TABLE "{name}"."alembicVersion" '
                '(version_num varchar(32) NOT NULL PRIMARY KEY)'))
        scoped = make_url(URL).update_query_dict({"options": f"-csearch_path={name},public"})
        engine = createEngine(scoped.render_as_string(hide_password=False), migrate=True)
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        admin.dispose()


def observer(sessions):
    node, now = "syn://thing/bluetooth-pg-" + uuid4().hex, datetime.now(UTC)
    PositionStore(sessions).bootstrap(node, geographic(51, 0, None, 100000, now, "timezone"))
    store = BluetoothStore(sessions)
    store.prepare(node)
    return store, node


def test_migrated_native_types_dual_ledger_and_unknown_claim_without_placeholder(postgres):
    store, node = observer(postgres)
    unknown = "syn://thing/unknown-bluetooth-" + uuid4().hex
    result = store.record(node, "reception", {"identity": {"isSyn": True,
        "claimedNodeRef": unknown}, "report": {"count": 5, "rssi": -70.0}}, operation_id="first")
    assert result["identityStatus"] == "claimed-unknown" and result["peerRef"] is None
    for key in ("receipt", "eventReceipt"):
        verify_receipt(result[key], result[key]["workspace_id"])
    assert result["receipt"]["payload"] == result["eventReceipt"]["payload"]
    with postgres() as session:
        assert session.execute(text('SELECT version_num FROM "alembicVersion"')).scalar_one() == "20261118_0021"
        assert session.execute(select(db.primitive["things"].c.id).where(
            db.primitive["things"].c.id == unknown)).first() is None
        assert session.execute(select(func.count()).select_from(db.chronicle).where(
            db.chronicle.c.eventRef == result["eventRef"])).scalar_one() == 2
    assert BluetoothStore(postgres).record(node, "reception", {"identity": {
        "isSyn": True, "claimedNodeRef": unknown}, "report": {"count": 5, "rssi": -70.0}},
        operation_id="first")["duplicate"]


def test_concurrent_same_operation_commits_once_and_stays_replayable(postgres):
    store, node = observer(postgres)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.record(node, "transmission-started",
            {"frame": {"format": "syn"}}, operation_id="same-cycle"), range(4)))
    assert sum(not result["duplicate"] for result in results) == 1
    assert len({result["emberRef"] for result in results}) == 1
    with postgres() as session:
        count = session.execute(select(func.count()).select_from(messages).where(
            messages.c.workspace_id == event_workspace(results[0]["eventRef"]))).scalar_one()
    assert count == 1


def test_concurrent_distinct_observations_keep_node_receipts_and_chronicle_ordered(postgres):
    store, node = observer(postgres)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: store.record(node, "reception",
            {"sequence": i}, operation_id="observation-" + str(i)), range(8)))
    assert len({result["eventRef"] for result in results}) == 8
    with postgres() as session:
        rows = session.execute(select(db.chronicle).where(db.chronicle.c.objectRef == node,
            db.chronicle.c.kind == "ledger").order_by(db.chronicle.c.sequenceNumber)).mappings().all()
    assert [row["sequenceNumber"] for row in rows] == list(range(1, 9))
    assert len({row["details"]["receiptHash"] for row in rows}) == 8


def test_known_peer_target_leaves_native_peer_knowledge_unchanged(postgres):
    store, node = observer(postgres)
    peer = "syn://thing/bluetooth-known-peer-" + uuid4().hex
    with postgres.begin() as session:
        session.execute(db.primitive["things"].insert().values(id=peer,
            name="Local known peer", typeRef="syn://type/device", knowledge={"retained": True}))
    result = store.record(node, "reception", {"identity": {"claimedNodeRef": peer, "isSyn": True}})
    assert result["identityStatus"] == "claimed-known" and result["identity"]["verified"] is False
    with postgres() as session:
        assert session.execute(select(db.primitive["things"].c.knowledge).where(
            db.primitive["things"].c.id == peer)).scalar_one() == {"retained": True}
        assert session.execute(select(db.eventTargets.c.role).where(
            db.eventTargets.c.eventRef == result["eventRef"], db.eventTargets.c.targetRef == peer)).scalar_one() == "advertised-peer"


def test_second_receipt_failure_rolls_back_native_objects_and_chronicle(postgres, monkeypatch):
    store, node = observer(postgres)
    before = store.ledger.head(workspace_for(node))
    append = store.ledger.appendInTransaction

    def fail(session, workspace, body):
        if workspace != workspace_for(node):
            raise RuntimeError("Event receipt failed")
        return append(session, workspace, body)

    monkeypatch.setattr(store.ledger, "appendInTransaction", fail)
    with pytest.raises(RuntimeError, match="Event receipt failed"):
        store.record(node, "reception", {"count": 1}, operation_id="retry")
    assert store.ledger.head(workspace_for(node)) == before
    with postgres() as session:
        assert session.execute(select(db.chronicle).where(db.chronicle.c.objectRef == node)).first() is None
    monkeypatch.setattr(store.ledger, "appendInTransaction", append)
    assert not store.record(node, "reception", {"count": 1}, operation_id="retry")["duplicate"]
