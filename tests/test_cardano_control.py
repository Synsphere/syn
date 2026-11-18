from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from syn.cardano.config import CardanoConfig, peer_address
from syn.config import P2PConfig, loadConfig


@pytest.mark.parametrize("value,expected", [
    ("trusted.example:3001", ("trusted.example", 3001)),
    ("localhost:3001", ("localhost", 3001)),
    ("127.0.0.1:1", ("127.0.0.1", 1)),
    ("[2001:db8::1]:65535", ("2001:db8::1", 65535)),
])
def test_explicit_trusted_endpoints(value, expected):
    assert peer_address(value) == expected


@pytest.mark.parametrize("value", [
    "https://trusted.example:3001", "user@trusted.example:3001",
    "trusted.example", "trusted.example:03001", "trusted.example:0",
    "trusted.example:65536", "Trusted.example:3001", "trusted.example:3001/path",
    "trusted.example:3001\n", "bad_name:3001", "999.0.0.1:3001",
    "127.0.0.01:3001", "0.0.0.0:3001", "224.0.0.1:3001",
    "[::]:3001", "[fe80::1%eth0]:3001", "2001:db8::1:3001", "[::1]",
])
def test_ambiguous_or_non_destination_endpoints_rejected(value):
    with pytest.raises(ValueError):
        peer_address(value)


@pytest.mark.parametrize("values", [
    {"enabled": "true"}, {"enabled": True}, {"networkMagic": True},
    {"networkMagic": -1}, {"networkMagic": 2**32}, {"peers": ["a:3001"]},
    {"peers": ("a:3001", "a:3001")}, {"peers": tuple(f"a{i}:3001" for i in range(9))},
    {"startSlot": 1}, {"startHash": "a"*64}, {"startSlot": True, "startHash": "a"*64},
    {"startSlot": 1, "startHash": "A"*64}, {"timeoutSeconds": float("nan")},
    {"keepaliveIntervalSeconds": float("inf")}, {"reconnectDelaySeconds": False},
    {"maxReconnectDelaySeconds": 1, "reconnectDelaySeconds": 2},
])
def test_config_bounds(values):
    with pytest.raises(ValueError):
        CardanoConfig(**values)


def test_config_loads_opt_in_and_checkpoint_pair(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[database]\nurl="postgresql+psycopg://test"\n')
    assert loadConfig(path).cardano == CardanoConfig()
    path.write_text(path.read_text() + '[cardano]\nenabled=true\n'
        'networkMagic=2\npeers=["trusted.example:3001"]\n'
        'startSlot=123\nstartHash="' + "a"*64 + '"\n')
    assert loadConfig(path).cardano == CardanoConfig(enabled=True, networkMagic=2,
        peers=("trusted.example:3001",), startSlot=123, startHash="a"*64)
    with pytest.raises(ValueError, match="Unknown"):
        CardanoConfig.from_dict({"trustAllPeers": True})
    with pytest.raises(ValueError, match="table"):
        CardanoConfig.from_dict([])
    with pytest.raises(ValueError, match="array"):
        CardanoConfig.from_dict({"peers": "trusted.example:3001"})


def test_status_disabled_without_database_or_node_identity():
    from syn.api import createApp
    sessions = Mock()
    app = createApp(sessions, {}, P2PConfig(enabled=False))
    with TestClient(app) as client:
        response = client.get("/cardano/status")
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "running": False, "node_ref": None,
        "network_magic": 764824073, "blockchain_ref": "syn://thing/cardano",
        "mode": "trusted-peer-observations", "peers": []}
    sessions.assert_not_called()
    with pytest.raises(ValueError, match="node identity"):
        createApp(sessions, {}, P2PConfig(enabled=False),
                  cardanoConfig=CardanoConfig(enabled=True, peers=("localhost:3001",)))


@pytest.mark.parametrize("fail_start", [False, True])
def test_api_lifecycle_prepares_node_before_cardano_and_stops_it_before_transport(monkeypatch, fail_start):
    import syn.api as api
    import syn.cardano.runtime as runtime
    events = []
    node = "syn://thing/test-node"
    settings = CardanoConfig(enabled=True, peers=("localhost:3001",))

    class Scheduler:
        def __init__(self, *_args, schedulerKey, **_kwargs):
            self.schedulerKey = schedulerKey
            self.fault = None
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

    class Cardano:
        def __init__(self, _sessions, node_ref, config):
            assert node_ref == node and config == settings
        async def start(self):
            events.append("cardano.start")
            if fail_start:
                raise ValueError("Cardano preparation failed")
        async def stop(self):
            events.append("cardano.stop")
        def check(self):
            pass
        def status(self):
            return {"enabled": True}

    monkeypatch.setattr(api, "ProcessScheduler", Scheduler)
    monkeypatch.setattr(api, "Libp2pNode", P2P)
    monkeypatch.setattr(runtime, "CardanoRuntime", Cardano)
    position = SimpleNamespace(nodeRef=node, config=SimpleNamespace(maxConcurrentJobs=1),
        prepare=lambda _manager: events.append("position.prepare"))
    app = api.createApp(Mock(), {}, positionRuntime=position, cardanoConfig=settings)
    if fail_start:
        with pytest.raises(ValueError, match="preparation failed"), TestClient(app):
            pass
    else:
        with TestClient(app) as client:
            assert client.get("/cardano/status").json() == {"enabled": True}
    assert events == ["scheduler.start", "position.prepare", "p2p.start", "cardano.start",
                      "cardano.stop", "p2p.stop", "scheduler.stop"]


def test_native_runtime_owns_cardano_inside_scheduler_lifetime():
    from syn.positions.runtime import NativePositionRuntime
    events = []
    native = NativePositionRuntime.__new__(NativePositionRuntime)
    native.ready, native.stopped = threading.Event(), threading.Event()
    native.fault = None
    native.runtime = SimpleNamespace(prepare=lambda _: events.append("position.prepare"))

    class Scheduler:
        running = True
        fault = None
        async def start(self, *, prepare):
            events.append("scheduler.start")
            prepare(self)
        async def stop(self):
            events.append("scheduler.stop")

    class Cardano:
        async def start(self):
            events.append("cardano.start")
        def check(self):
            events.append("cardano.check")
            native.stopped.set()
        async def stop(self):
            events.append("cardano.stop")

    native.scheduler, native.cardano = Scheduler(), Cardano()
    asyncio.run(native._serve())
    assert native.ready.is_set() and native.fault is None
    assert events == ["scheduler.start", "position.prepare", "cardano.start", "cardano.check",
                      "cardano.stop", "scheduler.stop"]
