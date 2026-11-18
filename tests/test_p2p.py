from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import socket
from unittest.mock import MagicMock, patch

import multiaddr
import trio

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
        assert {key: value for key, value in status.items() if key not in {"amino", "publication"}} == {
            "enabled": False,
            "running": False,
            "peerId": None,
            "listenAddresses": [],
            "bootstrapPeers": [],
            "mdnsEnabled": True,
            "discoveredPeers": [],
            "upnpEnabled": True,
            "upnpExternalIp": None,
        }
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


def test_upnp_is_enabled_on_the_libp2p_host() -> None:
    node = Libp2pNode(P2PConfig(
        mdnsEnabled=False,
        listenAddresses=("/ip4/0.0.0.0/tcp/4001",),
        upnpEnabled=True,
        aminoEnabled=False,
        bootstrapPeers=(),
    ))
    node._stop.set()
    host = MagicMock()
    host.get_id.return_value = "local-peer"
    host.get_addrs.return_value = [multiaddr.Multiaddr("/ip4/0.0.0.0/tcp/4001")]
    host.get_transport_addrs.return_value = host.get_addrs.return_value
    host.get_peerstore.return_value.valid_peer_ids.return_value = []
    host.upnp.get_external_ip.return_value = "203.0.113.20"

    @asynccontextmanager
    async def runningHost():
        yield

    host.run.return_value = runningHost()
    with patch("syn.p2p.new_host", return_value=host) as createHost:
        trio.run(node._serve)

    options = createHost.call_args.kwargs
    assert options["bootstrap"] == []
    assert options["enable_upnp"] is True
    assert set(options["sec_opt"]) == {"/noise"}
    assert set(options["muxer_opt"]) == {"/yamux/1.0.0"}
    assert node.status()["upnpExternalIp"] == "203.0.113.20"
