from datetime import UTC, datetime, timedelta

import pytest

from syn.radios.types import ProviderResult, RadioDevice


def candidate(chip="SX1302"):
    return RadioDevice("bridge", "spi0.0", "lora-concentrator", chip,
                       "spi", chipset=chip, evidence=("test",))


def report(*devices, ok=True):
    return (ProviderResult("bridge", "complete" if ok else "error",
                           tuple(devices), errorType=None if ok else "TimeoutError"),)


def test_first_empty_scan_creates_event_and_ember(state):
    store, node, _ = state
    result = store.record(node, report(), now=datetime.now(UTC))
    assert result["changed"] and result["count"] == 0
    current = store.current(node)
    assert current["revision"] == 1
    assert store.verify(node)["objects"] == 2


def test_repeat_scan_keeps_immutable_ember_and_adds_event(state):
    store, node, _ = state
    start = datetime.now(UTC)
    a = store.record(node, report(candidate()), now=start)
    b = store.record(node, report(candidate()), now=start+timedelta(seconds=1))
    assert a["changed"] and not b["changed"]
    assert a["inventoryEmberRef"] == b["inventoryEmberRef"]
    assert store.current(node)["revision"] == 1
    assert store.verify(node)["objects"] == 4


def test_removal_only_when_provider_scan_completed(state):
    store, node, _ = state
    store.record(node, report(candidate()))
    failed = store.record(node, report(ok=False))
    assert not failed["changed"] and failed["count"] == 1
    gone = store.record(node, report())
    assert gone["changed"] and gone["count"] == 0
    assert store.current(node)["revision"] == 2
    assert store.verify(node)["revision"] == 2


def test_device_capability_change_creates_new_immutable_ember(state):
    store, node, _ = state
    store.record(node, report(candidate()))
    prior = store.current(node)
    store.record(node, report(candidate("SX1303")))
    new = store.current(node)
    assert new["inventoryHash"] != prior["inventoryHash"]
    assert new["revision"] == 2
    assert new["deviceRefs"] != prior["deviceRefs"]


def test_refuses_invalid_scan_without_partial_write(state):
    store, node, _ = state
    store.record(node, report())
    current = store.current(node)
    with pytest.raises(ValueError):
        store.record(node, report() + report())
    assert store.current(node) == current


def test_refuses_missing_node(state):
    store, _, _ = state
    with pytest.raises(Exception):
        store.record("syn://thing/absent", report())


def test_ledger_rollback_does_not_create_native_objects(state, monkeypatch):
    store, node, sessions = state
    store.record(node, report(candidate()))
    original = store.current(node)
    original_receipt = store._receipt
    def fail(*args, **kwargs):
        raise RuntimeError("ledger down")
    monkeypatch.setattr(store, "_receipt", fail)
    with pytest.raises(RuntimeError, match="ledger down"):
        store.record(node, report())
    assert store.current(node) == original
    monkeypatch.setattr(store, "_receipt", original_receipt)
    assert store.verify(node)["revision"] == 1


def test_tampered_inventory_detected(state):
    from sqlalchemy import select
    from syn.positions.schema import primitive
    store, node, sessions = state
    store.record(node, report(candidate()))
    with sessions.begin() as session:
        row = session.execute(select(primitive["things"]).where(
            primitive["things"].c.id == store.inventory_ref(node))).mappings().one()
        corrupt = dict(row["knowledge"])
        corrupt["devices"] = {}
        session.execute(primitive["things"].update().where(
            primitive["things"].c.id == store.inventory_ref(node)).values(knowledge=corrupt))
    with pytest.raises(ValueError, match="digest mismatch"):
        store.verify(node)
