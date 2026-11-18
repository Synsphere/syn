"""Legacy pinned relays retain encrypted native delivery over real TCP/Noise."""
from __future__ import annotations

import copy
from functools import partial
import secrets

from libp2p.peer.peerinfo import info_from_p2p_addr
import pytest
import trio

from syn.agent.pubsub import NativePubSub
from syn import mesh_gossipsub as gossip
from syn_client.operator_protocol import canonical
from syn_client.pubsub import verify_message

from test_gossipsub_discovery import eventually, native_links, relay_directory  # noqa: F401
from test_pubsub import control, fixture  # noqa: F401 - explicit synthetic custody


def test_pinned_legacy_relays_exchange_an_encrypted_native_message(
    fixture, monkeypatch, relay_directory,
):
    servers, selected = fixture
    nodes = [NativePubSub(server) for server in servers]
    identities, peer_ids = [], []
    for index in range(2):
        identity = relay_directory / f"legacy-{index}.seed"
        identity.write_bytes(secrets.token_bytes(32))
        identity.chmod(0o600)
        identities.append(identity)
        peer_ids.append(gossip.peer_id(identity))

    configs = [{
        "profile": gossip.LEGACY_PROFILE, "scope": selected,
        "identity_path": str(identities[index]), "listen": "/ip4/127.0.0.1/tcp/0",
        "endpoints": [{"node": servers[index].expected_hash,
                       "socket": f"/synthetic/legacy-{index}.sock", "owner_uid": 1000 + index}],
        "peers": [{"peer_id": peer_ids[1 - index], "address": None,
                   "nodes": [servers[1 - index].expected_hash]}],
    } for index in range(2)]

    def no_dht(*_, **__):
        pytest.fail("Legacy pinned relays must not bootstrap public discovery")

    monkeypatch.setattr(gossip, "AminoDHTClient", no_dht)

    async def exercise():
        # The shared helper carries whichever native link version the relay
        # requests. Its only substitutions are sockets and synthetic root UID;
        # NativePubSub.attach, custody checks and signatures remain exercised.
        sockets, workers, errors, requests = native_links(monkeypatch, nodes, configs)
        relays = [None, gossip.GossipRelay(configs[1])]
        ready = [trio.Event(), trio.Event()]
        stopped = [trio.Event(), trio.Event()]
        forwarded = []

        async def run_relay(index):
            try:
                await relays[index].run(ready[index].set)
            finally:
                stopped[index].set()

        try:
            async with trio.open_nursery() as nursery:
                nursery.start_soon(run_relay, 1)
                try:
                    # Select the already listening target before constructing
                    # the dialling relay: its real configured address is used.
                    await eventually(lambda: relays[1].host is not None and
                                     bool(relays[1].host.get_addrs()), timeout=5)
                    target = info_from_p2p_addr(relays[1].host.get_addrs()[0])
                    assert str(target.peer_id) == peer_ids[1]
                    configs[0]["peers"][0]["address"] = str(target.addrs[0])
                    relays[0] = gossip.GossipRelay(configs[0])
                    real_forward = relays[0]._forward

                    def capture(sender, packet):
                        forwarded.append(copy.deepcopy(packet))
                        real_forward(sender, packet)

                    relays[0]._forward = capture
                    nursery.start_soon(run_relay, 0)
                    await eventually(lambda: all(event.is_set() for event in ready), timeout=8)
                    await eventually(lambda: all(node.ready.is_set() for node in nodes), timeout=3)

                    assert not errors
                    assert len(requests) == 2
                    assert {request["profile"] for request in requests} == {"syn.private-pubsub-link/1"}
                    assert all(set(request) == {"profile", "scope"} for request in requests)
                    for index, relay in enumerate(relays):
                        assert relay.dynamic is False and relay.dht is None
                        assert relay.active and not relay.fault.is_set()
                        assert relay.peers == {relays[1 - index].identity:
                            frozenset({servers[1 - index].expected_hash})}
                        assert relay.pubsub.admitted_peers == frozenset({relays[1 - index].identity})

                    payload = {"private_marker": secrets.token_hex(24), "profile": "legacy-loopback"}
                    accepted = await trio.to_thread.run_sync(partial(control, nodes[0], "publish",
                        {"topic": "syn://type/legacy-pinned-test", "value": payload}))
                    assert accepted[0]["result"]["relay_accepted"] is True
                    assert accepted[0]["result"]["peer_reception_proved"] is False
                    await eventually(lambda: bool(nodes[1].messages), timeout=5)
                    received = await trio.to_thread.run_sync(control, nodes[1], "receive")
                    entry, = received[0]["result"]["messages"]
                    assert entry["payload"] == payload
                    verify_message(entry["envelope"], selected,
                        expected_node=servers[0].expected_hash, expected_boot=servers[0].boot_id)
                    wire, = forwarded
                    assert wire["envelope"] == entry["envelope"]
                    assert "payload" not in wire["envelope"]
                    assert payload["private_marker"].encode() not in canonical(wire)
                    assert not errors and not any(node.fault.is_set() for node in nodes)
                finally:
                    for relay in relays:
                        if relay is not None:
                            relay.stopped.set()
                    with trio.fail_after(5):
                        for index, relay in enumerate(relays):
                            if relay is not None:
                                await stopped[index].wait()
        finally:
            for node in nodes:
                await trio.to_thread.run_sync(node.close)
            for connection in sockets:
                connection.close()
            for worker in workers:
                await trio.to_thread.run_sync(worker.join, 2)
                assert not worker.is_alive()

    trio.run(exercise)
