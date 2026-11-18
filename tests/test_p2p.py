from __future__ import annotations

import asyncio
import socket
from threading import Event
from unittest.mock import MagicMock, patch

import multiaddr
import pytest
import trio
from trio.testing import wait_all_tasks_blocked
from libp2p import new_host

from syn import P2PConfig
from syn.p2p import Libp2pNode


def test_libp2p_node_starts_listens_and_stops() -> None:
    async def exercise() -> None:
        node = Libp2pNode(
            P2PConfig(
                listenAddresses=("/ip4/127.0.0.1/tcp/0",),
                startupTimeoutSeconds=10,
                upnpEnabled=False,
                mdnsEnabled=False,
                aminoEnabled=False,
                bootstrapPeers=(),
            )
        )
        await node.start()
        running = node.status()
        assert running["running"] is True
        assert running["peerId"]
        assert running["listenAddresses"]
        assert running["mdnsEnabled"] is False
        assert running["amino"]["mode"] == "client"
        assert running["publication"]["state"] == "no-operator"
        assert all("/tcp/" in address for address in running["listenAddresses"])

        await node.stop()
        assert node.status()["running"] is False

    asyncio.run(exercise())


def test_default_node_listens_on_all_ipv4_and_ipv6_interfaces() -> None:
    async def exercise() -> None:
        node = Libp2pNode(P2PConfig(startupTimeoutSeconds=10, upnpEnabled=False,
                                  mdnsEnabled=False, aminoEnabled=False, bootstrapPeers=()))
        await node.start()
        try:
            addresses = node.status()["listenAddresses"]
            assert any(address.startswith("/ip4/0.0.0.0/tcp/") for address in addresses)
            assert any(address.startswith("/ip6/::/tcp/") for address in addresses)
        finally:
            await node.stop()

    asyncio.run(exercise())


def test_disabled_libp2p_node_does_not_start() -> None:
    async def exercise() -> None:
        node = Libp2pNode(P2PConfig(enabled=False, bootstrapPeers=()))
        await node.start()
        status = node.status()
        assert {key: value for key, value in status.items() if key not in {"amino", "publication", "upnp"}} == {
            "enabled": False,
            "running": False,
            "peerId": None,
            "listenAddresses": [],
            "bootstrapPeers": [],
            "mdnsEnabled": True,
            "discoveredPeers": [],
            "upnpEnabled": True,
            "upnpExternalIp": None,
            "publicPubsub": {"enabled": False, "running": False,
                             "default_network_id": "syn", "groups": []},
        }
        assert status["upnp"]["state"] == "disabled"
        assert status["upnp"]["enabled"] is False
        assert status["publication"]["state"] == "no-operator"

    asyncio.run(exercise())


def test_two_nodes_discover_each_other_over_mdns() -> None:
    async def exercise() -> None:
        firstPort, secondPort = _freeTcpPort(), _freeTcpPort()
        first = Libp2pNode(P2PConfig(
            listenAddresses=(f"/ip4/0.0.0.0/tcp/{firstPort}",),
            startupTimeoutSeconds=10,
            upnpEnabled=False,
            aminoEnabled=False,
            bootstrapPeers=(),
        ))
        second = Libp2pNode(P2PConfig(
            listenAddresses=(f"/ip4/0.0.0.0/tcp/{secondPort}",),
            startupTimeoutSeconds=10,
            upnpEnabled=False,
            aminoEnabled=False,
            bootstrapPeers=(),
        ))
        await first.start()
        await second.start()
        try:
            firstId = first.status()["peerId"]
            secondId = second.status()["peerId"]
            for _ in range(100):
                if (
                    secondId in first.status()["discoveredPeers"]
                    and firstId in second.status()["discoveredPeers"]
                ):
                    break
                await asyncio.sleep(0.1)
            assert secondId in first.status()["discoveredPeers"]
            assert firstId in second.status()["discoveredPeers"]
        finally:
            await second.stop()
            await first.stop()

    asyncio.run(exercise())


def _freeTcpPort() -> int:
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        return int(listener.getsockname()[1])


@pytest.mark.parametrize("accepted", [True, False])
def test_upnp_maps_only_ipv4_after_readiness_and_cleans_up(accepted) -> None:
    class Manager:
        def __init__(self):
            self.entered, self.release = Event(), Event()
            self.attempts, self.removals, self.mappings = [], [], []
            self.external = None

        async def discover(self):
            self.entered.set()
            while not self.release.is_set():
                await trio.sleep(0.01)
            self.external = "8.8.8.8"
            return True

        async def add_port_mapping(self, port, protocol):
            self.attempts.append((port, protocol))
            if accepted:
                self.mappings.append({"port": port, "protocol": protocol,
                                      "owned": True, "verified": True, "leaseSeconds": 3600})
            return accepted

        async def remove_port_mapping(self, port, protocol):
            self.removals.append((port, protocol))
            self.mappings = [entry for entry in self.mappings if entry["port"] != port]
            return True

        def get_external_ip(self):
            return self.external

        def status(self):
            return {"enabled": True, "state": "mapped" if self.mappings else "waiting",
                    "reason": None, "devices": 1, "lanAddress": "192.168.1.50",
                    "externalIp": self.external, "mappings": list(self.mappings)}

    async def exercise():
        manager = Manager()
        node = Libp2pNode(P2PConfig(mdnsEnabled=False, aminoEnabled=False,
            upnpEnabled=True, bootstrapPeers=(), startupTimeoutSeconds=10))
        with patch("syn.p2p.SynUpnpManager", return_value=manager), \
                patch("syn.p2p.new_host", wraps=new_host) as create_host:
            await node.start()
            try:
                # The listener is ready while SSDP discovery is still pending.
                assert await asyncio.to_thread(manager.entered.wait, 2)
                assert node.status()["running"] and not manager.release.is_set()
                assert not node._public_addresses(node._host)
                manager.release.set()
                for _ in range(100):
                    if node.status()["upnpExternalIp"]:
                        break
                    await asyncio.sleep(0.01)
                v4 = next(int(multiaddr.Multiaddr(address).value_for_protocol("tcp"))
                          for address in node.status()["listenAddresses"] if address.startswith("/ip4/"))
                assert manager.attempts == [(v4, "TCP")]
                announced = [str(value) for value in node._public_addresses(node._host)]
                assert announced == ([f"/ip4/8.8.8.8/tcp/{v4}"] if accepted else [])
                options = create_host.call_args.kwargs
                assert options["enable_upnp"] is False
                assert set(options["sec_opt"]) == {"/noise"}
                assert set(options["muxer_opt"]) == {"/yamux/1.0.0"}
            finally:
                await node.stop()
        assert manager.removals == (manager.attempts if accepted else [])
        assert node.status()["upnp"]["state"] == "stopped"
        assert node.status()["upnpExternalIp"] is None

    asyncio.run(exercise())


def test_upnp_excludes_loopback_ipv6_and_unrelated_bound_interfaces():
    host = MagicMock()
    host.get_transport_addrs.return_value = [multiaddr.Multiaddr(value) for value in (
        "/ip4/0.0.0.0/tcp/4001", "/ip4/192.168.1.50/tcp/4002",
        "/ip4/127.0.0.1/tcp/4003", "/ip6/::/tcp/4004",
        "/ip4/192.168.2.50/tcp/4005", "/ip4/0.0.0.0/udp/4006",
        "/ip4/0.0.0.0/tcp/0")]
    assert Libp2pNode._ipv4_mapping_ports(host, "192.168.1.50") == {4001, 4002}


@pytest.mark.parametrize("cleanup_succeeds", [True, False])
def test_upnp_maintenance_cleans_mappings_for_disappeared_listeners(cleanup_succeeds):
    class Manager:
        def __init__(self):
            self.mappings = {4001: {"port": 4001, "protocol": "TCP", "verified": False,
                                    "owned": True, "leaseSeconds": 0}}
            self.removals = []

        async def discover(self):
            return True

        async def add_port_mapping(self, port, protocol):
            self.mappings[port] = {"port": port, "protocol": protocol, "verified": True,
                                   "owned": True, "leaseSeconds": 3600}
            return True

        async def remove_port_mapping(self, port, protocol):
            self.removals.append((port, protocol))
            if port == 4001 and not cleanup_succeeds:
                return False
            self.mappings.pop(port, None)
            return True

        def status(self):
            return {"enabled": True, "state": "mapped", "reason": None,
                    "lanAddress": "192.168.1.50", "externalIp": "8.8.8.8",
                    "mappings": list(self.mappings.values())}

    async def exercise():
        manager = Manager()
        node = Libp2pNode(P2PConfig(mdnsEnabled=False, aminoEnabled=False, bootstrapPeers=()))
        node._upnp = manager
        host = MagicMock()
        host.get_transport_addrs.return_value = [multiaddr.Multiaddr("/ip4/0.0.0.0/tcp/4002")]
        async with trio.open_nursery() as nursery:
            nursery.start_soon(node._upnp_loop, host)
            await wait_all_tasks_blocked()
            assert manager.removals == [(4001, "TCP")]
            assert node._mapped_ports == {4002}
            assert node.status()["upnp"]["state"] == ("mapped" if cleanup_succeeds else "cleanup-failed")
            assert (4001 in manager.mappings) is not cleanup_succeeds
            nursery.cancel_scope.cancel()
        assert (4002, "TCP") in manager.removals
        assert node._mapped_ports == set()
        assert node.status()["upnp"]["state"] == ("stopped" if cleanup_succeeds else "cleanup-failed")

    trio.run(exercise)
