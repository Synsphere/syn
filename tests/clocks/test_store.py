"""Clock persistence contracts; SQLite exercises logic, not PostgreSQL triggers."""
import json

import pytest
from sqlalchemy import func, select

from syn.clocks import schema
from syn.clocks.types import ClockReading

NODE = "syn://thing/clock-contract-test"
BOOT = "6f00d39f-4ec6-4567-8596-c176cdaf318f"
WALL, MONO = 1_790_000_000_000_000_000, 100_000_000_000


def test_boot_records_unknown_estimate_and_verifies(clock_state):
    store, sessions, positions, _ = clock_state
    value = store.current(NODE)
    assert value["estimate"]["utcOffsetMs"] == 0
    assert value["estimate"]["precisionMs"] is None
    assert store.verify(NODE)["objects"] == 2
    positions.verify(NODE)


def test_reading_records_event_statistics_estimate_and_same_ledger(clock_state):
    store, sessions, positions, _ = clock_state
    result = store.observe(NODE, BOOT, ClockReading("ntp:test", "ntp", WALL+10**9, MONO+10**9, 25, 2))
    value = store.current(NODE)
    assert value["estimate"]["utcOffsetMs"] == 25
    assert value["emberRef"] == result["emberRef"]
    assert value["sources"]["ntp:test"]["samples"] == 1
    assert store.verify(NODE)["objects"] == 5
    positions.verify(NODE)


def test_old_session_cannot_write(clock_state):
    store, *_ = clock_state
    before = store.verify(NODE)
    assert store.observe(NODE, "obsolete", ClockReading("test", "external", WALL, MONO, 1, 1)) == {"status": "obsolete-session"}
    assert store.verify(NODE) == before


def test_restart_resets_belief_retains_history(clock_state):
    store, *_ = clock_state
    store.observe(NODE, BOOT, ClockReading("test", "external", WALL, MONO, 5, 1))
    before = store.verify(NODE)["objects"]
    store.begin(NODE, "6f00d39f-4ec6-4567-8596-c176cdaf318e", wall=WALL+10**9, mono=MONO+10**9)
    assert store.current(NODE)["estimate"]["precisionMs"] is None
    assert store.current(NODE)["estimate"]["utcOffsetMs"] == 0
    assert store.verify(NODE)["objects"] == before+2


def test_ledger_failure_rolls_back_entire_observation(clock_state, monkeypatch):
    store, sessions, *_ = clock_state
    before = store.current(NODE)
    with sessions() as session:
        count = session.scalar(select(func.count()).select_from(schema.records))
    original = store.ledger.appendInTransaction
    calls = 0
    def fail_later(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("ledger unavailable")
        return original(*args, **kwargs)
    monkeypatch.setattr(store.ledger, "appendInTransaction", fail_later)
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        store.observe(NODE, BOOT, ClockReading("test", "external", WALL, MONO, 5, 1))
    assert store.current(NODE) == before
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(schema.records)) == count
    store.verify(NODE)


def test_native_ember_tampering_is_detected(clock_state):
    from syn.positions.schema import primitive
    store, sessions, *_ = clock_state
    ref = store.current(NODE)["emberRef"]
    with sessions.begin() as session:
        session.execute(primitive["embers"].update().where(primitive["embers"].c.id == ref).values(knowledge={"changed": True}))
    with pytest.raises(ValueError, match="altered"):
        store.verify(NODE)
