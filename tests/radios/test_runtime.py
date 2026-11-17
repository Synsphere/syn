from __future__ import annotations

from unittest.mock import Mock, patch

from syn.radios.config import RadioConfig
from syn.radios.jobs import RADIO_DISCOVER, schedule_key
from syn.positions.runtime import PositionRuntime
from syn.positions.config import PositionConfig


def test_radio_config_from_syn_config(tmp_path):
    from syn.config import loadConfig
    config = tmp_path / "syn.toml"
    config.write_text('[database]\nurl="postgresql+psycopg://localhost/test"\n'
                      '[radio]\nrescanSeconds=45\nuseSoapySDR=false\n')
    loaded = loadConfig(config)
    assert loaded.radio.rescanSeconds == 45
    assert not loaded.radio.useSoapySDR


def test_disabled_runtime_registers_no_radio_handler(tmp_path):
    runtime = PositionRuntime(Mock(), "postgresql+psycopg://invalid",
        PositionConfig(stateDirectory=str(tmp_path)), identity="1234",
        radioConfig=RadioConfig(enabled=False))
    assert RADIO_DISCOVER not in runtime.handlers()


def test_enabled_runtime_registers_recurring_job(tmp_path):
    sessions = Mock()
    runtime = PositionRuntime(sessions, "postgresql+psycopg://invalid",
        PositionConfig(stateDirectory=str(tmp_path)), identity="1234",
        radioConfig=RadioConfig(rescanSeconds=300))
    assert RADIO_DISCOVER in runtime.handlers()
    scheduler = Mock()
    scheduler.schedulerKey = "default"
    runtime.store.current = Mock(return_value=[{"spaceRef": "syn://thing/space-wgs84-geodetic"}])
    runtime.store.verify = Mock()
    runtime.store.upgrade_geographic_type = Mock()
    runtime.clocks.prepare = Mock()
    with patch("syn.radios.store.RadioStore") as radio_store, patch(
            "syn.radios.discovery.RadioDiscovery") as discovery:
        discovery.return_value.scan.return_value = ()
        runtime.prepare(scheduler)
    runtime.clocks.prepare.assert_called_once_with(scheduler)
    radio_store.return_value.record.assert_called_once_with(runtime.nodeRef, ())
    scheduler.every.assert_any_call(schedule_key(runtime.nodeRef), RADIO_DISCOVER,
                                   {"schedulerKey": "default"}, 300)
