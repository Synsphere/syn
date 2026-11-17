"""Radio discovery augments, rather than replaces, clock-enabled node startup.

SQLite is an isolated projection here, not a production backend qualification.
No test probes real hardware, creates an operator key or contacts an NTP server.
"""
from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from syn.clocks.config import ClockConfig
from syn.clocks.runtime import GPSD, LOCAL, NTP
from syn.clocks.types import ClockReading
from syn.config import DatabaseConfig, SynConfig, loadConfig
from syn.positions.config import PositionConfig
from syn.positions.jobs import DISCOVER, POLL
from syn.positions.runtime import NativePositionRuntime, PositionRuntime
from syn.positions.types import geographic
from syn.radios.config import RadioConfig
from syn.radios.jobs import RADIO_DISCOVER
from syn.radios.store import RadioStore
from syn.radios.types import ProviderResult, RadioDevice

NODE = "syn://thing/combined-awareness-test"
WALL = 1_790_000_000_000_000_000
MONO = 100_000_000_000


def reports():
    device = RadioDevice("fixture", "phy0", "wifi", "Fixture Wi-Fi", "pci",
                         canReceive=True, canTransmit=True, evidence=("synthetic",))
    return (ProviderResult("fixture", "complete", (device,)),)


@pytest.fixture
def combined(tmp_path, monkeypatch, ledger_module):
    from syn import recurrence
    from syn.clocks import schema as clock_schema
    from syn.positions import schema
    from syn.positions import runtime as position_runtime

    engine = create_engine("sqlite:///" + str(tmp_path / "combined.db"))
    schema.metadata.create_all(engine)
    recurrence.metadata.create_all(engine)
    ledger_module.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions.begin() as session:
        schema.seed_types(session)
        clock_schema.seed_types(session)
    now = datetime.fromtimestamp(WALL / 1e9, UTC)
    monkeypatch.setattr(position_runtime, "bootstrap", lambda config:
                        geographic(51, 0, None, 100000, now, "timezone"))
    monkeypatch.setattr("syn.clocks.store.paired_time", lambda: (WALL, MONO, 0.001))
    scan = Mock(return_value=reports())
    monkeypatch.setattr("syn.radios.discovery.RadioDiscovery.scan", scan)
    config = ClockConfig(ntpServers=("time.example.test",), gpsdHosts=(),
                         chrony=False, hardware=False)
    runtime = PositionRuntime(sessions, "unused",
                              PositionConfig(nodeRef=NODE, stateDirectory=str(tmp_path)),
                              identity="fixture", clockConfig=config,
                              radioConfig=RadioConfig(useCommands=False))
    scheduler = Mock(schedulerKey="combined-awareness")
    yield runtime, scheduler, scan, sessions
    engine.dispose()


def test_combined_prepare_persists_clock_and_radio_on_one_ledger(combined):
    runtime, scheduler, scan, sessions = combined
    runtime.prepare(scheduler)
    clock = runtime.clocks.store.current(NODE)
    assert clock["estimate"]["utcOffsetMs"] == 0
    assert clock["estimate"]["precisionMs"] is None
    assert clock["estimate"]["uncertaintyMs"] is None
    radios = RadioStore(sessions)
    assert radios.current(NODE)["revision"] == 1
    scan.assert_called_once()
    names = [call.args[1] for call in scheduler.every.call_args_list]
    assert names.count(NTP) == names.count(LOCAL) == names.count(DISCOVER) == names.count(RADIO_DISCOVER) == 1
    heads = [runtime.store.verify(NODE), runtime.clocks.store.verify(NODE), radios.verify(NODE)]
    assert len({(head["sequence"], head["hash"]) for head in heads}) == 1


def test_radio_and_clock_observations_remain_independently_verifiable(combined):
    runtime, scheduler, _, sessions = combined
    runtime.prepare(scheduler)
    radios = RadioStore(sessions)
    before = runtime.clocks.store.current(NODE)
    radios.record(NODE, reports())
    assert runtime.clocks.store.current(NODE) == before
    reading = ClockReading("ntp:test", "ntp", WALL + 10**9, MONO + 10**9,
                           offsetMs=12.5, uncertaintyMs=2)
    runtime.clocks.store.observe(NODE, runtime.clocks.sessionId, reading)
    inventory = radios.current(NODE)
    radios.record(NODE, (ProviderResult("fixture", "complete"),))
    assert radios.current(NODE)["revision"] == inventory["revision"] + 1
    heads = [runtime.store.verify(NODE), runtime.clocks.store.verify(NODE), radios.verify(NODE)]
    assert len({(head["sequence"], head["hash"]) for head in heads}) == 1


def test_disabling_radios_does_not_disable_clock_preparation(combined):
    runtime, scheduler, scan, sessions = combined
    runtime.radioConfig = RadioConfig(enabled=False)
    runtime.prepare(scheduler)
    scan.assert_not_called()
    assert RadioStore(sessions).current(NODE) is None
    assert runtime.clocks.store.current(NODE)["estimate"]["precisionMs"] is None
    names = [call.args[1] for call in scheduler.every.call_args_list]
    assert NTP in names and LOCAL in names and DISCOVER in names
    assert RADIO_DISCOVER not in names
    assert RADIO_DISCOVER not in runtime.handlers()


def test_radio_restart_preserves_position_and_starts_new_clock_session(combined, monkeypatch):
    runtime, scheduler, _, sessions = combined
    runtime.prepare(scheduler)
    position = runtime.store.current(NODE)
    old_clock = runtime.clocks.store.current(NODE)
    old_inventory = RadioStore(sessions).current(NODE)
    monkeypatch.setattr("syn.positions.runtime.bootstrap", Mock(side_effect=AssertionError("reset prior")))
    restarted = PositionRuntime(sessions, "unused", runtime.config, identity="fixture",
                                clockConfig=runtime.clocks.config, radioConfig=runtime.radioConfig)
    restarted.prepare(scheduler)
    assert restarted.store.current(NODE) == position
    clock = restarted.clocks.store.current(NODE)
    assert clock["sessionId"] != old_clock["sessionId"]
    assert clock["estimate"]["utcOffsetMs"] == 0
    assert clock["estimate"]["precisionMs"] is None
    assert RadioStore(sessions).current(NODE)["revision"] == old_inventory["revision"]
    assert RadioStore(sessions).verify(NODE)["hash"] == restarted.clocks.store.verify(NODE)["hash"]


def test_gps_poll_preserves_clock_policy_and_session(combined):
    runtime, _, _, _ = combined
    handlers = runtime.handlers()
    assert {NTP, LOCAL, GPSD, DISCOVER, POLL, RADIO_DISCOVER} <= handlers.keys()
    assert handlers[POLL].keywords["clockSettings"] == runtime.clocks.config.to_dict()
    assert handlers[POLL].keywords["clockSessionId"] == runtime.clocks.sessionId
    assert handlers[RADIO_DISCOVER].args == ("unused", runtime.radioConfig.to_dict(), NODE)


def test_config_round_trip_keeps_independent_clock_and_radio_settings(tmp_path):
    path = tmp_path / "syn.toml"
    path.write_text('[database]\nurl="postgresql+psycopg://localhost/syn"\n'
                    '[clock]\nntpServers=["clock.example.test"]\nntpSeconds=128\n'
                    '[radio]\nrescanSeconds=600\nuseCommands=false\n')
    config = loadConfig(path)
    assert config.clock.ntpServers == ("clock.example.test",)
    assert config.clock.ntpSeconds == 128
    assert config.radio.rescanSeconds == 600
    assert config.radio.useCommands is False


def test_native_runtime_receives_both_configurations(tmp_path, monkeypatch):
    import syn.config as config_module
    import syn.database as database

    path = tmp_path / "position.toml"
    path.write_text("# loaded by the mocked configuration boundary")
    monkeypatch.delenv("SYN_POSITION_CONFIG", raising=False)
    monkeypatch.delenv("SYN_DATABASE_URL", raising=False)
    config = SynConfig(DatabaseConfig("unused"), position=PositionConfig(nodeRef=NODE),
                       clock=ClockConfig(ntpServers=("clock.example.test",)),
                       radio=RadioConfig(enabled=False))
    monkeypatch.setattr(config_module, "loadConfig", Mock(return_value=config))
    engine, sessions = Mock(), Mock()
    monkeypatch.setattr(database, "createEngine", Mock(return_value=engine))
    monkeypatch.setattr(database, "createSessionFactory", Mock(return_value=sessions))
    scheduler_module = types.ModuleType("syn.scheduler")
    scheduler_module.ProcessScheduler = Mock()
    monkeypatch.setitem(sys.modules, "syn.scheduler", scheduler_module)
    server = types.SimpleNamespace(directory=tmp_path, expected_hash="operator-fixture")
    native = NativePositionRuntime(server)
    try:
        assert native.runtime.radioConfig is config.radio
        assert native.runtime.clocks.config is config.clock
        registered = scheduler_module.ProcessScheduler.call_args.args[1]
        assert NTP in registered and RADIO_DISCOVER not in registered
    finally:
        native.close()
    engine.dispose.assert_called_once()


@pytest.mark.parametrize("api_only", [False, True])
def test_server_keeps_operator_setup_and_passes_both_configs(tmp_path, monkeypatch, api_only):
    from syn import startup
    import syn.positions.runtime as position_runtime

    api = types.ModuleType("syn.api")
    api.createApp = Mock()
    feed = types.ModuleType("syn.feed_jobs")
    feed.defaultJobHandlers = Mock(return_value={})
    monkeypatch.setitem(sys.modules, "syn.api", api)
    monkeypatch.setitem(sys.modules, "syn.feed_jobs", feed)
    path = Path(__file__).resolve().parents[2] / "src/syn/server.py"
    spec = importlib.util.spec_from_file_location("syn._radio_server_test", path)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    config = SynConfig(DatabaseConfig("unused"))
    monkeypatch.setattr(entry, "loadConfig", Mock(return_value=config))
    monkeypatch.setattr(entry, "createEngine", Mock())
    monkeypatch.setattr(entry, "createSessionFactory", Mock())
    monkeypatch.setattr(entry.uvicorn, "run", Mock())
    monkeypatch.setattr(startup, "handoff_windows", Mock(return_value=False))
    monkeypatch.setattr(startup, "ensure_database_config", Mock())
    monkeypatch.setattr(startup, "ensure_operator", Mock(return_value=tmp_path / "operator.json"))
    monkeypatch.setattr(startup, "ManagedOperator", Mock())
    factory = Mock()
    factory.return_value.handlers.return_value = {}
    monkeypatch.setattr(position_runtime, "PositionRuntime", factory)
    entry.main(["--api-only"] if api_only else [])
    assert factory.call_args.kwargs["clockConfig"] is config.clock
    assert factory.call_args.kwargs["radioConfig"] is config.radio
    if api_only:
        startup.ensure_operator.assert_not_called()
        assert "operatorRuntime" not in api.createApp.call_args.kwargs
    else:
        startup.ensure_operator.assert_called_once_with(None)
        operator = startup.ManagedOperator.return_value
        assert factory.call_args.kwargs["identity"] == operator.server.expected_hash
        assert api.createApp.call_args.kwargs["operatorRuntime"] is operator
    entry.createEngine.return_value.dispose.assert_called_once()
