"""Opt-in PostgreSQL migration, constraints and concurrent ledger qualification.

Set SYN_CARDANO_TEST_DATABASE_URL to a disposable database with DDL rights.
Every run owns a new random schema and removes only that schema on completion.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from syn.cardano import schema as db
from syn.cardano.protocol import decode_block_header
from syn.cardano.store import CardanoStore, event_workspace
from syn.communication import MessageStore
from syn.positions.store import PositionStore, workspace_for
from syn.positions.types import geographic


URL = os.environ.get("SYN_CARDANO_TEST_DATABASE_URL")
pytestmark = [pytest.mark.database,
              pytest.mark.skipif(not URL, reason="Real Cardano PostgreSQL opt-in URL is not configured")]
RAW = bytes.fromhex((Path(__file__).parent / "fixtures/cardano/conway-v11.block").read_text())
HEADER = asdict(decode_block_header(RAW))
ANCHOR = {"slot": HEADER["slot"] - 1, "hash": HEADER["previous_hash"]}
MAGIC, PEER = 764824073, "trusted-relay.example:3001"


@pytest.fixture(scope="module")
def postgres():
    from syn.database import createEngine
    name = "syn_cardano_test_" + uuid4().hex
    admin = create_engine(URL)
    engine = None
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{name}"'))
            connection.execute(text(f'CREATE TABLE "{name}"."alembicVersion" '
                '(version_num varchar(32) NOT NULL PRIMARY KEY)'))
        scoped = make_url(URL).update_query_dict({"options": f"-csearch_path={name},public"})
        engine = createEngine(scoped.render_as_string(hide_password=False), migrate=True)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        yield sessions
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        admin.dispose()


def observer(sessions):
    node, now = "syn://thing/cardano-pg-" + uuid4().hex, datetime.now(UTC)
    PositionStore(sessions).bootstrap(node, geographic(51, 0, None, 100000, now, "timezone"))
    store = CardanoStore(sessions)
    store.prepare(node, MAGIC, [PEER])
    store.intersect(node, MAGIC, PEER, ANCHOR)
    return store, node


def test_full_migration_dual_ledger_and_rollback(postgres):
    store, node = observer(postgres)
    with postgres() as session:
        assert session.execute(text('SELECT version_num FROM "alembicVersion"')).scalar_one() == "20261118_0021"
    result = store.apply(node, MAGIC, PEER, HEADER, RAW)
    assert result["event_ref"] == "syn://event/" + HEADER["block_hash"]
    assert store.rollback(node, MAGIC, PEER, ANCHOR)["retracted"] == 1
    assert store.status(node, MAGIC)["peers"][0]["active_blocks"] == 0
    own = store.ledger.page(workspace_for(node), 0, 100)["items"]
    event = store.ledger.page(event_workspace(result["event_ref"]), 0, 100)["items"]
    observations = [row for row in own if row["payload"].get("profile") == "syn.cardano-observation/1"]
    assert len(observations) == 2
    for row in observations:
        assert any(other["message_id"] == row["message_id"] and other["payload"] == row["payload"] for other in event)


def test_concurrent_duplicate_apply_is_one_observation(postgres):
    store, node = observer(postgres)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.apply(node, MAGIC, PEER, HEADER, RAW), range(4)))
    assert sorted(result["action"] for result in results) == ["apply", "duplicate", "duplicate", "duplicate"]
    assert len({result["ember_ref"] for result in results}) == 1


def test_two_observers_can_share_event_ledger_without_deadlock(postgres):
    first, first_node = observer(postgres)
    second, second_node = observer(postgres)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first.apply, first_node, MAGIC, PEER, HEADER, RAW)
        b = pool.submit(second.apply, second_node, MAGIC, PEER, HEADER, RAW)
        results = [a.result(timeout=20), b.result(timeout=20)]
    assert results[0]["event_ref"] == results[1]["event_ref"]
    assert results[0]["ember_ref"] != results[1]["ember_ref"]


def test_database_guards_immutable_objects_chronicles_and_unledgered_state(postgres):
    store, node = observer(postgres)
    result = store.apply(node, MAGIC, PEER, HEADER, RAW)
    updates = [
        db.primitive["events"].update().where(db.primitive["events"].c.id == result["event_ref"]).values(knowledge={}),
        db.primitive["embers"].update().where(db.primitive["embers"].c.id == result["ember_ref"]).values(knowledge={}),
        db.records.delete().where(db.records.c.emberRef == result["ember_ref"]),
        db.chronicle.delete().where(db.chronicle.c.objectRef == node),
        db.followers.update().where(db.followers.c.nodeRef == node).values(revision=999),
        db.chain.update().where(db.chain.c.observationRef == result["ember_ref"]).values(active=False),
    ]
    for statement in updates:
        with pytest.raises(DBAPIError):
            with postgres.begin() as session:
                session.execute(statement)
    assert store.status(node, MAGIC)["peers"][0]["active_blocks"] == 1


def test_deferred_dual_receipt_constraint_rejects_missing_event_write(postgres, monkeypatch):
    store, node = observer(postgres)
    original = store.ledger.appendInTransaction
    event_id = "syn://event/" + HEADER["block_hash"]
    before = store.ledger.head(workspace_for(node))
    def fabricate(session, workspace, body):
        if workspace == event_workspace(event_id):
            return {"workspace_id": workspace, "sequence": 999999, "hash": "0" * 64}
        return original(session, workspace, body)
    monkeypatch.setattr(store.ledger, "appendInTransaction", fabricate)
    with pytest.raises(DBAPIError, match="both ledger receipts"):
        store.apply(node, MAGIC, PEER, HEADER, RAW)
    assert MessageStore(postgres).head(workspace_for(node)) == before
    assert store.status(node, MAGIC)["peers"][0]["point"] == ANCHOR


def test_failed_second_ledger_append_rolls_back_all_local_changes(postgres, monkeypatch):
    store, node = observer(postgres)
    original = store.ledger.appendInTransaction
    before = store.ledger.head(workspace_for(node))
    def unavailable(session, workspace, body):
        if workspace == event_workspace("syn://event/" + HEADER["block_hash"]):
            raise RuntimeError("simulated event ledger failure")
        return original(session, workspace, body)
    monkeypatch.setattr(store.ledger, "appendInTransaction", unavailable)
    with pytest.raises(RuntimeError):
        store.apply(node, MAGIC, PEER, HEADER, RAW)
    assert store.ledger.head(workspace_for(node)) == before
    assert store.status(node, MAGIC)["peers"][0]["active_blocks"] == 0
    with postgres() as session:
        assert session.execute(select(func.count()).select_from(db.records.join(
            db.followers, db.records.c.followerId == db.followers.c.id)).where(db.followers.c.nodeRef == node)).scalar_one() == 0
