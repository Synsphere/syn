"""Expected serial-port unavailability must not poison the node scheduler."""
from contextlib import contextmanager
from datetime import UTC, datetime
import sys
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import select

from syn.positions import jobs, schema
from syn.positions.config import PositionConfig
from syn.positions.providers import serial as serial_provider
from syn.positions.providers.base import Device, ProviderUnavailable
from syn.positions.types import geographic
from syn.recurrence import metadata

NODE = "syn://thing/serial-recovery-test"


def test_wsl_ghost_serial_ports_require_explicit_selection(monkeypatch):
    serial_module = ModuleType("serial")
    tools_module = ModuleType("serial.tools")
    ports_module = ModuleType("serial.tools.list_ports")
    ports_module.comports = lambda include_links=True: [
        SimpleNamespace(device="/dev/ttyS0", vid=None, pid=None, product=None,
                        description="legacy serial", serial_number=None, hwid="n/a"),
        SimpleNamespace(device="/dev/ttyUSB0", vid=0x1234, pid=0x5678,
                        product="GPS", description="GPS", serial_number="test", hwid="USB"),
    ]
    monkeypatch.setitem(sys.modules, "serial", serial_module)
    monkeypatch.setitem(sys.modules, "serial.tools", tools_module)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", ports_module)
    monkeypatch.setattr(serial_provider, "_inside_wsl", lambda: True)
    result = serial_provider.probe(PositionConfig())
    assert [d.address for d in result.devices] == ["/dev/ttyUSB0"]
    explicitly_selected = serial_provider.probe(PositionConfig(serialPorts=("/dev/ttyS0",)))
    assert [d.address for d in explicitly_selected.devices] == ["/dev/ttyS0"]


def test_serial_port_open_failure_becomes_provider_unavailable(monkeypatch):
    pyserial = ModuleType("serial")
    class SerialException(Exception):
        pass
    observed = []
    class StubPort:
        def __init__(self, **kwargs):
            self.closed = False
            observed.append(self)
        def open(self):
            raise SerialException("physical device and sensitive path")
        def close(self):
            self.closed = True
    pyserial.SerialException = SerialException
    pyserial.Serial = StubPort
    monkeypatch.setitem(sys.modules, "serial", pyserial)
    with pytest.raises(ProviderUnavailable, match="inaccessible") as error:
        serial_provider.poll(Device("serial", "/dev/ttyS0"), PositionConfig(serialBaudRates=(9600,)))
    assert type(error.value.__cause__).__name__ == "SerialException"
    assert observed[0].closed
    assert "physical device" not in str(error.value)


def test_unexpected_serial_code_errors_still_raise(monkeypatch):
    pyserial = ModuleType("serial")
    pyserial.SerialException = type("SerialException", (Exception,), {})
    class StubPort:
        def __init__(self, **kwargs):
            pass
        def open(self):
            raise ValueError("real programming error")
        def close(self):
            pass
    pyserial.Serial = StubPort
    monkeypatch.setitem(sys.modules, "serial", pyserial)
    with pytest.raises(ValueError, match="real programming error"):
        serial_provider.poll(Device("serial", "/dev/ttyS0"), PositionConfig(serialBaudRates=(9600,)))


def test_unavailable_receiver_records_diagnostic_and_recovers(state, monkeypatch):
    store, sessions, _ = state
    metadata.create_all(sessions.kw["bind"])
    store.bootstrap(NODE, geographic(51, 0, None, 100000, datetime.now(UTC), "timezone"))
    @contextmanager
    def connect(url):
        assert url == "test-database-not-a-credential"
        yield sessions
    monkeypatch.setattr(jobs, "sessions_for", connect)
    config = PositionConfig()
    device = Device("serial", "/dev/ttyUSB0", options={"confirmed": False})
    monkeypatch.setattr(jobs, "discover", lambda settings:
        ([device], [{"provider": "serial", "status": "candidates"}]))
    args = ("test-database-not-a-credential", config.to_dict(), NODE)
    jobs.run_discovery(*args, schedulerKey="a")
    def unavailable(*_):
        try:
            raise OSError("secret host device details")
        except OSError as error:
            raise ProviderUnavailable("device is busy") from error
    monkeypatch.setattr(jobs, "poll", unavailable)
    result = jobs.run_poll(*args, deviceId=device.id, schedulerKey="a")
    assert result == {"status": "unavailable", "readings": 0, "reason": "OSError"}
    with sessions() as session:
        assert session.execute(select(schema.devices.c.lastError)).scalar_one() == "unavailable:OSError"
        assert session.execute(select(schema.devices.c.active)).scalar_one() is True
    # Discovery is not proof that the receiver became readable.
    jobs.run_discovery(*args, schedulerKey="a")
    with sessions() as session:
        assert session.execute(select(schema.devices.c.lastError)).scalar_one() == "unavailable:OSError"
    monkeypatch.setattr(jobs, "poll", lambda device, settings: ([], dict(device.options)))
    assert jobs.run_poll(*args, deviceId=device.id, schedulerKey="a")["status"] == "read"
    with sessions() as session:
        assert session.execute(select(schema.devices.c.lastError)).scalar_one() is None
