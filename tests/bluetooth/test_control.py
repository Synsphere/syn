from __future__ import annotations

import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from syn.bluetooth.config import BluetoothConfig
from syn.config import P2PConfig, loadConfig


@pytest.mark.parametrize("values", [
    {"enabled": "yes"}, {"formats": ["syn"]}, {"formats": ()},
    {"formats": ("raw",)}, {"formats": ("syn", "syn")}, {"formats": ("ibeacon",)},
    {"formats": ("altbeacon",)}, {"companyId": True}, {"companyId": 65535},
    {"frameDurationSeconds": 0}, {"listenSeconds": 0},
    {"startupJitterSeconds": -1}, {"cycleJitterSeconds": float("inf")},
    {"inventoryRefreshSeconds": float("nan")}, {"retrySeconds": "5"},
    {"backendTimeoutSeconds": True}, {"observationWindowSeconds": -1},
    {"maxPendingObservations": 0}, {"maxPendingObservations": 8193}, {"maxAdapters": 65},
])
def test_config_rejects_unbounded_or_unbranded_settings(values):
    with pytest.raises(ValueError, match="bluetooth|AltBeacon"):
        BluetoothConfig(**values)


def test_config_loads_and_example_enables_radio_runtime(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[database]\nurl="postgresql+psycopg://test"\n')
    assert loadConfig(path).bluetooth == BluetoothConfig()
    path.write_text(path.read_text() + '[bluetooth]\nenabled=true\nformats=["syn", "syn-ibeacon"]\nlistenSeconds=30\n')
    assert loadConfig(path).bluetooth == BluetoothConfig(enabled=True, formats=("syn", "syn-ibeacon"), listenSeconds=30)
    example = loadConfig(Path(__file__).parents[2] / "config.example.toml")
    assert example.bluetooth.enabled and example.cardano.enabled
    assert example.p2p.enabled and example.p2p.aminoEnabled and example.p2p.publicPubsubEnabled
    with pytest.raises(ValueError, match="Unknown"):
        BluetoothConfig.from_dict({"rawBytes": "ff"})
    with pytest.raises(ValueError, match="table"):
        BluetoothConfig.from_dict([])
    with pytest.raises(ValueError, match="array"):
        BluetoothConfig.from_dict({"formats": "syn"})


def test_status_endpoint_disabled_without_node_or_database():
    from syn.api import createApp
    sessions = Mock()
    app = createApp(sessions, {}, P2PConfig(enabled=False))
    with TestClient(app) as client:
        response = client.get("/bluetooth/status")
        assert response.status_code == 200
        assert response.json()["state"] == "disabled"
        assert response.json()["adapters"] == []
        app.state.awarenessFault = RuntimeError("Native radio cleanup is unconfirmed")
        assert client.get("/bluetooth/status").status_code == 200
        assert client.get("/v1/capabilities").status_code == 503
    sessions.assert_not_called()
    with pytest.raises(ValueError, match="node identity"):
        createApp(sessions, {}, P2PConfig(enabled=False), bluetoothConfig=BluetoothConfig(enabled=True))


@pytest.mark.parametrize("fails", [False, True])
def test_native_lifecycle_prepares_node_and_releases_bluetooth_before_other_services(fails):
    from syn.positions.runtime import NativePositionRuntime
    events = []
    native = NativePositionRuntime.__new__(NativePositionRuntime)
    native.ready, native.stopped = threading.Event(), threading.Event()
    native.fault = None
    native.runtime = SimpleNamespace(prepare=lambda _: events.append("node.prepare"))

    class Scheduler:
        running = True
        fault = None
        async def start(self, *, prepare):
            events.append("scheduler.start")
            prepare(self)
        async def stop(self):
            events.append("scheduler.stop")

    class Service:
        def __init__(self, name):
            self.name = name
        async def start(self):
            events.append(self.name + ".start")
            if self.name == "bluetooth" and fails:
                raise RuntimeError("Ledger preparation failed")
        def check(self):
            events.append(self.name + ".check")
            if self.name == "bluetooth":
                native.stopped.set()
        async def stop(self):
            events.append(self.name + ".stop")

    native.scheduler = Scheduler()
    native.cardano, native.bluetooth = Service("cardano"), Service("bluetooth")
    asyncio.run(native._serve())
    assert native.ready.is_set()
    assert (native.fault is not None) == fails
    assert events[:4] == ["scheduler.start", "node.prepare", "cardano.start", "bluetooth.start"]
    assert events[-3:] == ["bluetooth.stop", "cardano.stop", "scheduler.stop"]


@pytest.mark.parametrize("fails", [False, True])
def test_api_lifecycle_orders_bluetooth_and_cleans_up_failed_start(monkeypatch, fails):
    import syn.api as api
    import syn.bluetooth.runtime as module
    events = []
    node = "syn://thing/api-test-node"
    settings = BluetoothConfig(enabled=True)

    class Scheduler:
        def __init__(self, *_args, schedulerKey, **_kwargs):
            self.schedulerKey, self.fault = schedulerKey, None
        async def start(self, *, prepare):
            events.append("scheduler.start")
            prepare(self)
        async def stop(self):
            events.append("scheduler.stop")

    class P2P:
        def __init__(self, *_args):
            pass
        async def start(self):
            events.append("p2p.start")
        async def stop(self):
            events.append("p2p.stop")
        def check(self):
            pass

    class Bluetooth:
        def __init__(self, _sessions, node_ref, config):
            assert node_ref == node and config == settings
        async def start(self):
            events.append("bluetooth.start")
            if fails:
                raise RuntimeError("Native observation preparation failed")
        async def stop(self):
            events.append("bluetooth.stop")
        def check(self):
            pass

    monkeypatch.setattr(api, "ProcessScheduler", Scheduler)
    monkeypatch.setattr(api, "Libp2pNode", P2P)
    monkeypatch.setattr(module, "BluetoothRuntime", Bluetooth)
    position = SimpleNamespace(nodeRef=node, config=SimpleNamespace(maxConcurrentJobs=1),
        prepare=lambda _: events.append("node.prepare"))
    app = api.createApp(Mock(), {}, P2PConfig(enabled=False), positionRuntime=position,
                        bluetoothConfig=settings)
    if fails:
        with pytest.raises(RuntimeError, match="preparation"):
            with TestClient(app):
                pass
    else:
        with TestClient(app):
            assert events[-1] == "bluetooth.start"
    assert events == ["scheduler.start", "node.prepare", "p2p.start", "bluetooth.start",
                      "bluetooth.stop", "p2p.stop", "scheduler.stop"]
