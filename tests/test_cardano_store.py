"""Durable Cardano contract tests using SQLite projections and the real ledger.

These are not PostgreSQL trigger qualification or independent Cardano consensus
validation. Synthetic successor headers exercise persistence after wire decoding.
"""
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
import copy
import hashlib
import importlib.util
import json

import cbor2
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from syn.cardano import schema as db
from syn.cardano.protocol import decode_block_header
from syn.cardano.store import (CardanoStore, CardanoStoreConflict, CardanoStoreError,
                               event_workspace)
from syn.communication import MessageStore, metadata as ledger_metadata, messages, heads
from syn.positions.store import workspace_for
from syn_client.protocol import GENESIS_HASH, verify_receipt


NODE = "syn://thing/cardano-test-observer"
MAGIC = 764824073
PEERS = ["relay-one.example:3001", "relay-two.example:3001"]
NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)
RAW = bytes.fromhex((Path(__file__).parent / "fixtures/cardano/conway-v11.block").read_text())
REAL = asdict(decode_block_header(RAW))
ANCHOR = {"slot": REAL["slot"] - 1, "hash": REAL["previous_hash"]}


def synthetic_block(slot, number, previous, *, branch=0):
    value = cbor2.loads(RAW)
    value[1][0][0][0:3] = [number, slot, bytes.fromhex(previous) if previous else None]
    value[1][0][1] = value[1][0][1][:-1] + bytes([branch])
    raw = cbor2.dumps(value)
    return asdict(decode_block_header(raw)), raw


@pytest.fixture
def state(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "cardano.db"))
    db.metadata.create_all(engine)
    ledger_metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions.begin() as session:
        db.seed_types(session)
        session.execute(db.primitive["things"].insert().values(id=NODE, name="Observer",
            typeRef="syn://type/device"))
        session.execute(db.position_nodes.insert().values(nodeRef=NODE, ledgerWorkspace=workspace_for(NODE),
            createdAt=NOW, probeReport=[]))
    store = CardanoStore(sessions)
    store.prepare(NODE, MAGIC, PEERS)
    yield store, sessions
    engine.dispose()


def begin(store, peer=PEERS[0], point=ANCHOR):
    return store.intersect(NODE, MAGIC, peer, point, observed_at=NOW)


def append(store, header=REAL, raw=RAW, peer=PEERS[0]):
    return store.apply(NODE, MAGIC, peer, header, raw, observed_at=NOW)


def receipt_chain(sessions, workspace):
    with sessions() as session:
        rows = session.execute(select(messages).where(messages.c.workspace_id == workspace)
            .order_by(messages.c.sequence)).mappings().all()
        head = session.execute(select(heads).where(heads.c.workspace_id == workspace)).mappings().one()
    previous = GENESIS_HASH
    for sequence, row in enumerate(rows, 1):
        receipt = {"schema_version": 1, **dict(row)}
        verify_receipt(receipt, workspace)
        assert receipt["sequence"] == sequence
        assert receipt["previous_hash"] == previous
        previous = receipt["hash"]
    assert head["sequence"] == len(rows) and head["hash"] == previous
    return rows


def test_seed_hierarchy_cardano_thing_and_repeat_prepare(state):
    store, sessions = state
    store.prepare(NODE, MAGIC, PEERS)
    with sessions() as session:
        assert session.execute(select(func.count()).select_from(db.followers)).scalar_one() == 2
        cardano = session.execute(select(db.primitive["things"]).where(
            db.primitive["things"].c.id == "syn://thing/cardano")).mappings().one()
        assert cardano["name"] == "Cardano" and cardano["typeRef"] == "syn://type/blockchain"
        edges = set(session.execute(select(db.typeParents.c.childRef, db.typeParents.c.parentRef)).all())
        assert ("syn://type/cardano-block", "syn://type/blockchain-block") in edges
        assert ("syn://type/blockchain-block", "syn://type/event") in edges
        assert ("syn://type/cardano-block-observation", "syn://type/observation-result") in edges
    assert store.intersection_points(NODE, MAGIC, PEERS[0]) == []


def test_exact_blockhash_event_and_same_observation_in_both_ledgers(state):
    store, sessions = state
    begin(store)
    result = append(store)
    event_ref = "syn://event/" + REAL["block_hash"]
    assert result["event_ref"] == event_ref
    with sessions() as session:
        event = session.execute(select(db.primitive["events"])).mappings().one()
        ember = session.execute(select(db.primitive["embers"])).mappings().one()
        record = session.execute(select(db.records)).mappings().one()
        native = session.execute(select(db.chronicle).order_by(db.chronicle.c.objectRef)).mappings().all()
        assert event["id"] == event_ref and event["typeRef"] == "syn://type/cardano-block"
        assert event["occurredAt"] is None
        assert event["knowledge"]["slot"] == REAL["slot"]
        assert ember["knowledge"]["observedAt"] == NOW.isoformat()
        assert ember["knowledge"]["blockCborSha256"] == hashlib.sha256(RAW).hexdigest()
        assert ember["knowledge"]["blockBytes"] == len(RAW)
        assert record["emberRef"] == result["ember_ref"]
        assert {row["objectRef"] for row in native} == {NODE, event_ref}
        assert {row["details"]["emberRef"] for row in native} == {ember["id"]}
        assert session.execute(select(db.blocks.c.headerCbor)).scalar_one() == REAL["cbor"]
    own = receipt_chain(sessions, workspace_for(NODE))
    event = receipt_chain(sessions, event_workspace(event_ref))
    observation = next(row for row in own if row["payload"].get("emberRef") == result["ember_ref"])
    assert event[0]["payload"] == observation["payload"]
    assert event[0]["message_id"] == observation["message_id"]
    assert event[0]["hash"] != observation["hash"]
    assert store.intersection_points(NODE, MAGIC, PEERS[0])[0] == {"slot": REAL["slot"], "hash": REAL["block_hash"]}


def test_replayed_block_after_commit_has_no_duplicate_native_or_ledger_rows(state):
    store, sessions = state
    begin(store)
    result = append(store)
    before = store.ledger.head(workspace_for(NODE))
    replay = append(CardanoStore(sessions))
    assert replay["action"] == "duplicate" and replay["ember_ref"] == result["ember_ref"]
    assert store.ledger.head(workspace_for(NODE)) == before
    with sessions() as session:
        assert session.execute(select(func.count()).select_from(db.records)).scalar_one() == 1
        assert session.execute(select(func.count()).select_from(db.chronicle)).scalar_one() == 2


def test_each_peer_observes_same_event_and_rollbacks_remain_source_specific(state):
    store, sessions = state
    for peer in PEERS:
        begin(store, peer)
    first, second = append(store), append(store, peer=PEERS[1])
    assert first["event_ref"] == second["event_ref"] and first["ember_ref"] != second["ember_ref"]
    result = store.rollback(NODE, MAGIC, PEERS[0], ANCHOR, observed_at=NOW)
    assert result["retracted"] == 1
    status = store.status(NODE, MAGIC)
    assert [peer["active_blocks"] for peer in status["peers"]] == [0, 1]
    assert status["peers"][0]["point"] == ANCHOR
    with sessions() as session:
        assert session.execute(select(func.count()).select_from(db.primitive["events"])).scalar_one() == 1
        observations = session.execute(select(db.primitive["embers"])).mappings().all()
        retraction = next(row for row in observations if row["knowledge"]["action"] == "rollback")
        assert retraction["knowledge"]["retracts"] == first["ember_ref"]
        assert retraction["knowledge"]["peer"] == PEERS[0]
    third = append(store)
    assert third["ember_ref"] not in (first["ember_ref"], second["ember_ref"])
    assert len(receipt_chain(sessions, event_workspace(first["event_ref"]))) == 4


def test_fork_rollbacks_preserve_events_then_accept_new_successor(state):
    store, sessions = state
    begin(store)
    first = append(store)
    second_header, second_raw = synthetic_block(REAL["slot"] + 20, REAL["block_number"] + 1, REAL["block_hash"])
    second = append(store, second_header, second_raw)
    rollback = store.rollback(NODE, MAGIC, PEERS[0], {"slot": REAL["slot"], "hash": REAL["block_hash"]}, observed_at=NOW)
    assert rollback["retracted"] == 1
    fork_header, fork_raw = synthetic_block(second_header["slot"], second_header["block_number"], REAL["block_hash"], branch=1)
    fork = append(store, fork_header, fork_raw)
    assert fork["event_ref"] != second["event_ref"]
    with sessions() as session:
        assert session.execute(select(func.count()).select_from(db.blocks)).scalar_one() == 3
        assert session.execute(select(func.count()).select_from(db.records)).scalar_one() == 4
    assert len(receipt_chain(sessions, event_workspace(second["event_ref"]))) == 2
    assert len(receipt_chain(sessions, event_workspace(first["event_ref"]))) == 1


def test_reconnect_intersection_retracts_abandoned_suffix_and_origin_is_durable(state):
    store, sessions = state
    begin(store)
    append(store)
    result = CardanoStore(sessions).intersect(NODE, MAGIC, PEERS[0], ANCHOR, observed_at=NOW)
    assert result["retracted"] == 1 and result["action"] == "intersect"
    store.rollback(NODE, MAGIC, PEERS[0], None, observed_at=NOW)
    assert CardanoStore(sessions).intersection_points(NODE, MAGIC, PEERS[0]) == [None]
    assert store.status(NODE, MAGIC)["peers"][0]["initialized"] is True


def test_unknown_older_point_is_explicit_anchor_but_unknown_in_observed_chain_is_conflict(state):
    store, sessions = state
    begin(store)
    append(store)
    before = store.ledger.head(workspace_for(NODE))
    with pytest.raises(CardanoStoreConflict):
        store.rollback(NODE, MAGIC, PEERS[0], {"slot": REAL["slot"], "hash": "0" * 64})
    assert store.ledger.head(workspace_for(NODE)) == before
    old = {"slot": ANCHOR["slot"] - 100, "hash": "1" * 64}
    assert store.rollback(NODE, MAGIC, PEERS[0], old)["retracted"] == 1
    assert store.intersection_points(NODE, MAGIC, PEERS[0]) == [old]


@pytest.mark.parametrize("fault", ["event-receipt", "chronicle", "checkpoint"])
def test_failure_rolls_back_event_ember_both_ledgers_and_checkpoint(state, monkeypatch, fault):
    store, sessions = state
    begin(store)
    before = store.ledger.head(workspace_for(NODE))
    append_original = store.ledger.appendInTransaction
    if fault in ("event-receipt", "checkpoint"):
        def broken(session, workspace, body):
            if (fault == "event-receipt" and workspace == event_workspace("syn://event/" + REAL["block_hash"])) or (
                    fault == "checkpoint" and body["payload"].get("action") == "apply"):
                raise RuntimeError("injected persistence failure")
            return append_original(session, workspace, body)
        monkeypatch.setattr(store.ledger, "appendInTransaction", broken)
    else:
        from sqlalchemy import event
        def broken(_conn, _cursor, statement, _params, _context, _many):
            if statement.startswith('INSERT INTO "objectChronicle"'):
                raise RuntimeError("injected native ledger failure")
        event.listen(sessions.kw["bind"], "before_cursor_execute", broken)
    try:
        with pytest.raises(RuntimeError):
            append(store)
    finally:
        if fault == "chronicle":
            event.remove(sessions.kw["bind"], "before_cursor_execute", broken)
    assert store.ledger.head(workspace_for(NODE)) == before
    with sessions() as session:
        for table in (db.blocks, db.records, db.chain, db.primitive["events"], db.primitive["embers"], db.chronicle):
            assert session.execute(select(func.count()).select_from(table)).scalar_one() == 0
    assert store.status(NODE, MAGIC)["peers"][0]["point"] == ANCHOR


def test_rollback_failure_does_not_retract_one_ledger_or_advance_cursor(state, monkeypatch):
    store, sessions = state
    begin(store)
    result = append(store)
    before = store.ledger.head(workspace_for(NODE))
    original = store.ledger.appendInTransaction
    def broken(session, workspace, body):
        if workspace == event_workspace(result["event_ref"]):
            raise RuntimeError("event ledger unavailable")
        return original(session, workspace, body)
    monkeypatch.setattr(store.ledger, "appendInTransaction", broken)
    with pytest.raises(RuntimeError):
        store.rollback(NODE, MAGIC, PEERS[0], ANCHOR)
    assert store.ledger.head(workspace_for(NODE)) == before
    assert store.status(NODE, MAGIC)["peers"][0]["active_blocks"] == 1
    with sessions() as session:
        assert session.execute(select(func.count()).select_from(db.records)).scalar_one() == 1


@pytest.mark.parametrize("change", ["slot", "block_number", "previous_hash", "era", "hash", "body"])
def test_native_api_rechecks_header_facts_and_fetched_block_before_persistence(state, change):
    store, sessions = state
    begin(store)
    header, raw = copy.deepcopy(REAL), RAW
    if change in ("slot", "block_number"):
        header[change] += 1
    elif change == "previous_hash":
        header[change] = "0" * 64
    elif change == "era":
        header[change] = "babbage"
    elif change == "hash":
        header["block_hash"] = "0" * 64
    else:
        raw = b"\x80"
    with pytest.raises(CardanoStoreError):
        append(store, header, raw)
    with sessions() as session:
        assert session.execute(select(func.count()).select_from(db.blocks)).scalar_one() == 0


def test_predecessor_conflict_blocks_source_without_advancing(state):
    store, sessions = state
    begin(store)
    header, raw = synthetic_block(REAL["slot"], REAL["block_number"], "0" * 64)
    with pytest.raises(CardanoStoreConflict):
        append(store, header, raw)
    assert store.status(NODE, MAGIC)["peers"][0]["point"] == ANCHOR


def test_recent_spaced_intersections_include_original_anchor_without_unbounded_result(state):
    store, sessions = state
    begin(store)
    parent = REAL["previous_hash"]
    history = []
    for index in range(40):
        header, raw = synthetic_block(REAL["slot"] + index * 20, REAL["block_number"] + index, parent)
        append(store, header, raw)
        parent = header["block_hash"]
        history.append({"slot": header["slot"], "hash": parent})
    points = store.intersection_points(NODE, MAGIC, PEERS[0], limit=8)
    assert len(points) <= 8 and points[0] == history[-1] and points[-1] == ANCHOR
    assert history[-5] in points and history[-9] in points
    assert len({json.dumps(point, sort_keys=True) for point in points}) == len(points)


def test_migration_is_frozen_and_history_downgrade_is_explicit():
    path = Path(__file__).parents[1] / "src/syn/alembic/versions/20261118_0021_cardano_observer.py"
    spec = importlib.util.spec_from_file_location("cardano_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "20261117_0020"
    assert not any("syn.cardano" in line for line in path.read_text().splitlines() if line.startswith("from "))
    assert all(name in "\n".join(module.STATEMENTS) for name in (
        '"cardanoFollowers"', '"cardanoBlocks"', '"cardanoObservations"', '"cardanoPeerChain"'))
    assert all(not text(statement)._bindparams for statement in (*module.STATEMENTS, *module.INTEGRITY))
    with pytest.raises(RuntimeError, match="append-only"):
        module.downgrade()
