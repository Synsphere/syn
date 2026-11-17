"""Lifecycle and registered-handler integration with synthetic provider output."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from syn.positions import jobs, schema
from syn.positions.config import PositionConfig
from syn.positions.providers.base import Device
from syn.positions.types import geographic, Reading
from syn.recurrence import metadata, schedules

NODE = "syn://thing/runtime-test"


@pytest.fixture
def handlers(state, monkeypatch):
    store, sessions, ledger = state
    # Only metadata projections are added; Jobs themselves remain scheduler-owned.
    metadata.create_all(sessions.kw["bind"])
    now = datetime.now(UTC)
    store.bootstrap(NODE, geographic(51, 0, None, 100000, now, "timezone"))
    @contextmanager
    def connect(url):
        assert url == "test-database-not-a-credential"
        yield sessions
    monkeypatch.setattr(jobs, "sessions_for", connect)
    return store, sessions


def test_discovery_idempotent_and_poll_uses_ledger(handlers, monkeypatch):
    store, sessions = handlers
    config = PositionConfig()
    device = Device("gpsd", "local receiver", options={"endpoint":"127.0.0.1:2947", "path":"/dev/test"})
    monkeypatch.setattr(jobs,"discover",lambda config:([device],[{"provider":"gpsd:127.0.0.1:2947","status":"available"}]))
    for _ in range(2):
        jobs.run_discovery("test-database-not-a-credential",config.to_dict(),NODE,schedulerKey="a")
    with sessions() as s:
        assert len(s.execute(select(schedules)).all()) == 1
    now=datetime.now(UTC)
    reading=Reading(device.id,now,now,geographic(51.5,-.1,None,4,now,device.id),"2d")
    monkeypatch.setattr(jobs,"poll",lambda device,config:([reading],device.options))
    result=jobs.run_poll("test-database-not-a-credential",config.to_dict(),NODE,deviceId=device.id,schedulerKey="a")
    assert result["readings"]==1 and result["decisions"]=={"replace":1}
    assert store.verify(NODE)["objects"]==3


def test_disabled_provider_cancels_old_and_queued_work(handlers,monkeypatch):
    store,sessions=handlers
    config=PositionConfig()
    device=Device("serial","/dev/test",options={"confirmed":False})
    monkeypatch.setattr(jobs,"discover",lambda config:([device],[{"provider":"serial","status":"candidates"}]))
    jobs.run_discovery("test-database-not-a-credential",config.to_dict(),NODE,schedulerKey="a")
    disabled=replace(config,serialProbe=False)
    monkeypatch.setattr(jobs,"poll",Mock(side_effect=AssertionError("Disabled device was opened")))
    assert jobs.run_poll("test-database-not-a-credential",disabled.to_dict(),NODE,deviceId=device.id,schedulerKey="a")["status"]=="disabled"
    monkeypatch.setattr(jobs,"discover",lambda config:([],[]))
    jobs.run_discovery("test-database-not-a-credential",disabled.to_dict(),NODE,schedulerKey="a")
    with sessions() as s:
        assert s.execute(select(schedules.c.enabled)).scalar_one() is False
        assert s.execute(select(schema.devices.c.active)).scalar_one() is False


def test_inventory_error_keeps_retry_and_successful_unplug_disables(handlers,monkeypatch):
    _,sessions=handlers
    config=PositionConfig()
    device=Device("serial","/dev/test",options={"confirmed":True,"baudrate":9600})
    monkeypatch.setattr(jobs,"discover",lambda config:([device],[{"provider":"serial","status":"candidates"}]))
    jobs.run_discovery("test-database-not-a-credential",config.to_dict(),NODE,schedulerKey="a")
    monkeypatch.setattr(jobs,"discover",lambda config:([],[{"provider":"serial","status":"unavailable"}]))
    jobs.run_discovery("test-database-not-a-credential",config.to_dict(),NODE,schedulerKey="a")
    with sessions() as s:
        assert s.execute(select(schedules.c.enabled)).scalar_one() is True
    monkeypatch.setattr(jobs,"discover",lambda config:([],[{"provider":"serial","status":"no-devices"}]))
    jobs.run_discovery("test-database-not-a-credential",config.to_dict(),NODE,schedulerKey="a")
    with sessions() as s:
        assert s.execute(select(schedules.c.enabled)).scalar_one() is False


def test_runtime_bootstraps_before_scheduling_and_restart_preserves_position(state,now,monkeypatch,tmp_path):
    from syn.positions import runtime
    store,sessions,_=state
    scheduler=Mock(schedulerKey="node-test")
    config=PositionConfig(nodeRef=NODE,stateDirectory=str(tmp_path))
    prior=geographic(51,0,None,100000,now,"timezone")
    monkeypatch.setattr(runtime,"bootstrap",lambda config:prior)
    node=runtime.PositionRuntime(sessions,"unused",config,identity="test-id")
    node.prepare(scheduler)
    saved=node.store.current(NODE)
    scheduler.every.assert_called_once()
    assert saved[0]["revision"]==1
    monkeypatch.setattr(runtime,"bootstrap",Mock(side_effect=AssertionError("Prior was reset")))
    node.prepare(scheduler)
    assert saved==node.store.current(NODE)


def test_installation_id_is_stable_and_refuses_corruption(tmp_path):
    from syn.positions.runtime import installation_id
    first=installation_id(tmp_path)
    assert installation_id(tmp_path)==first
    (tmp_path/"position-node-id").write_text("corrupt")
    with pytest.raises(ValueError):
        installation_id(tmp_path)


def test_native_position_activation_is_explicit(tmp_path, monkeypatch):
    from syn.positions.runtime import native_position_configured

    monkeypatch.delenv("SYN_POSITION_CONFIG", raising=False)
    monkeypatch.delenv("SYN_DATABASE_URL", raising=False)
    assert not native_position_configured(tmp_path)
    (tmp_path / "position.toml").write_text("# explicit configuration\n")
    assert native_position_configured(tmp_path)
    (tmp_path / "position.toml").unlink()
    monkeypatch.setenv("SYN_DATABASE_URL", "postgresql+psycopg://localhost/syn")
    assert native_position_configured(tmp_path)
