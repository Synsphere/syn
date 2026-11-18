"""Real group-provider discovery, native delegation, and encrypted GossipSub."""
from __future__ import annotations

from contextlib import AsyncExitStack
import copy
from functools import partial
import hashlib
from pathlib import Path
import secrets
import socket
import tempfile
import threading
from uuid import uuid4

from libp2p.kad_dht.pb.kademlia_pb2 import Message
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.pubsub.gossipsub import PROTOCOL_ID_V11
from multiaddr import Multiaddr
import pytest
import trio
import varint

from syn.agent import pubsub as native_pubsub
from syn.amino import AminoDHTClient
from syn.mesh_gossipsub import AUTH_PROFILE, AUTH_PROTOCOL, PROFILE, GossipRelay
from syn.p2p_defaults import AMINO_PROTOCOL
from syn.peer_record_transport import read_frame, write_frame
from syn_client import pubsub_client
from syn_client.operator_protocol import MAX_FRAME_BYTES, canonical, parse, receive
from syn_client.pubsub import verify_message, verify_relay_proof

from test_amino import read_server_message, secure_host
from test_pubsub import control, fixture  # noqa: F401 - shared synthetic custody


class WireDHT:
    """A small wire server: retains provider records under their exact keys."""

    def __init__(self, host):
        self.host = host
        self.providers = {}
        self.announcements = []
        self.lookups = []
        self.visible_peer = None
        self.pubsub_streams = []
        host.set_stream_handler(AMINO_PROTOCOL, self.handle)
        # A public DHT contact that also supports GossipSub must still not
        # receive the relay's private subscriptions or roster publications.
        host.set_stream_handler(PROTOCOL_ID_V11, self.observe_pubsub)

    async def observe_pubsub(self, stream):
        self.pubsub_streams.append(stream.muxed_conn.peer_id)
        await stream.reset()

    async def handle(self, stream):
        try:
            with trio.fail_after(3):
                request = await read_server_message(stream)
                remote = stream.muxed_conn.peer_id
                if request.type == Message.ADD_PROVIDER:
                    assert len(request.providerPeers) == 1
                    provider = request.providerPeers[0]
                    assert provider.id == remote.to_bytes()
                    retained = Message.Peer()
                    retained.CopyFrom(provider)
                    self.providers.setdefault(request.key, {})[provider.id] = retained
                    self.announcements.append((request.key, remote))
                    return  # Fire-and-forget, as on the public DHT.
                assert request.type in (Message.FIND_NODE, Message.GET_PROVIDERS)
                response = Message(type=request.type)
                if request.type == Message.GET_PROVIDERS:
                    self.lookups.append((request.key, remote))
                    for peer, provider in self.providers.get(request.key, {}).items():
                        if self.visible_peer is None or peer == self.visible_peer:
                            response.providerPeers.add().CopyFrom(provider)
                payload = response.SerializeToString()
                await stream.write(varint.encode(len(payload)) + payload)
        finally:
            await stream.close()


def native_links(monkeypatch, nodes, configs):
    """Exercise real v2 NativePubSub.attach over sockets with synthetic custody."""
    sockets, workers, errors, requests, available = [], [], [], [], {}
    monkeypatch.setattr(native_pubsub, "peer_uid", lambda conn: 0)
    for node, config in zip(nodes, configs):
        child, parent = socket.socketpair()
        child.settimeout(10)
        parent.settimeout(5)
        sockets.extend((child, parent))
        endpoint = config["endpoints"][0]
        available[endpoint["socket"]] = (parent, endpoint["owner_uid"])

        def attach(node=node, child=child):
            try:
                request = receive(child)
                requests.append(request)
                node.attach(child, request)
            except BaseException as error:
                errors.append(error)
            finally:
                child.close()

        worker = threading.Thread(target=attach, daemon=True)
        worker.start()
        workers.append(worker)

    def connect(path, owner_uid):
        parent, expected_uid = available.pop(str(path))
        assert owner_uid == expected_uid
        return parent

    monkeypatch.setattr(pubsub_client, "private_connect", connect)
    return sockets, workers, errors, requests


async def eventually(predicate, *, timeout=10, detail=lambda: None):
    with trio.move_on_after(timeout) as deadline:
        while not predicate():
            await trio.sleep(0.01)
    assert not deadline.cancelled_caught, detail()


@pytest.fixture
def relay_directory():
    # The managed workspace can have a shared writable ancestor. Use a private
    # real temporary directory so the production seed-file checks stay active.
    with tempfile.TemporaryDirectory(prefix="syn-group-discovery-", dir="/tmp") as path:
        yield Path(path)


def test_relays_discover_group_providers_and_authenticate_before_gossip(
    fixture, monkeypatch, relay_directory,
):
    servers, selected = fixture
    nodes = [native_pubsub.NativePubSub(server) for server in servers]
    sorted_members = sorted(selected["members"], key=lambda cert: cert["body"]["node_public_key_hash"])
    group_digest = hashlib.sha256(canonical({**selected, "members": sorted_members})).digest()
    key = b"\x12\x20" + group_digest
    other_scope = {**selected, "workspace_id": str(uuid4()), "members": sorted_members}
    other_key = b"\x12\x20" + hashlib.sha256(canonical(other_scope)).digest()
    assert key != other_key

    async def exercise():
        bootstrap, impostor, different_group = secure_host(), secure_host(), secure_host()
        server = WireDHT(bootstrap)
        replayed_to = set()
        wrong_group_requests = []
        relays, ready, stopped = [], [], []
        native_errors, sockets, workers, native_requests = [], [], [], []
        forwarded = []

        async def replay_native_proof(stream):
            try:
                with trio.fail_after(3):
                    request = parse(await read_frame(stream, MAX_FRAME_BYTES))
                    assert request["profile"] == AUTH_PROFILE
                    assert request["group_hash"] == group_digest.hex()
                    remote = stream.muxed_conn.peer_id
                    # Return a real other Agent's delegation, signed for that
                    # Agent's genuine relay. The current Noise peer is the
                    # impostor, so the copied proof must not grant admission.
                    owner = next(relay for relay in relays if relay.identity != remote)
                    await eventually(lambda: bool(owner.proofs), timeout=2)
                    await write_frame(stream, canonical(owner._auth_packet()), MAX_FRAME_BYTES)
                    replayed_to.add(remote)
            finally:
                await stream.close()

        async def wrong_group_auth(stream):
            wrong_group_requests.append(stream.muxed_conn.peer_id)
            await stream.reset()

        impostor.set_stream_handler(AUTH_PROTOCOL, replay_native_proof)
        different_group.set_stream_handler(AUTH_PROTOCOL, wrong_group_auth)
        async with AsyncExitStack() as stack:
            for host in (bootstrap, impostor, different_group):
                await stack.enter_async_context(host.run(
                    listen_addrs=[Multiaddr("/ip4/127.0.0.1/tcp/0")]))
            seed = str(bootstrap.get_addrs()[0])
            configs = []
            for index, node in enumerate(nodes):
                identity = relay_directory / f"relay-{index}.seed"
                identity.write_bytes(secrets.token_bytes(32))
                identity.chmod(0o600)
                config = {
                    "profile": PROFILE, "scope": selected,
                    "identity_path": str(identity), "listen": "/ip4/127.0.0.1/tcp/0",
                    "endpoints": [{"node": node.server.expected_hash,
                        "socket": f"/synthetic/agent-{index}.sock", "owner_uid": 1000 + index}],
                    "discovery": {"bootstrapPeers": [seed], "allowPrivateAddresses": True,
                        "requestTimeoutSeconds": 2, "lookupTimeoutSeconds": 3,
                        "bootstrapRefreshSeconds": 60, "providerRepublishSeconds": 60,
                        "discoveryIntervalSeconds": 1},
                }
                assert "peers" not in config
                relay = GossipRelay(config)
                assert relay.peers == {} and relay.dial == []
                assert relay.provider_key == key
                assert relay.group_hash == group_digest.hex()
                real_forward = relay._forward

                def capture(sender, packet, real_forward=real_forward):
                    forwarded.append(copy.deepcopy(packet))
                    real_forward(sender, packet)

                relay._forward = capture
                relays.append(relay)
                configs.append(config)
                ready.append(trio.Event())
                stopped.append(trio.Event())
            sockets, workers, native_errors, native_requests = native_links(monkeypatch, nodes, configs)

            # Both untrusted providers enter through actual ADD_PROVIDER RPCs.
            # One uses this group hash, the other a different workspace hash.
            for host, provider_key in ((impostor, key), (different_group, other_key)):
                dht = AminoDHTClient(host, [seed], timeout=2, allow_private=True)
                assert await dht.bootstrap() == 1
                assert await dht.provide(provider_key, host.get_addrs()) == 1
            await eventually(lambda: other_key in server.providers and key in server.providers)
            server.visible_peer = impostor.get_id().to_bytes()

            async def run_relay(index):
                try:
                    await relays[index].run(ready[index].set)
                finally:
                    stopped[index].set()

            try:
                async with trio.open_nursery() as nursery:
                    for index in range(2):
                        nursery.start_soon(run_relay, index)
                    try:
                        await eventually(lambda: len(replayed_to) == 2 and all(
                            r.discovery_status()["rejectedCandidates"] > 0 for r in relays),
                            detail=lambda: [r.discovery_status() for r in relays])
                        assert not native_errors
                        assert all(not r.active and r.peers == {} for r in relays)
                        assert not any(event.is_set() for event in ready)

                        # Release the genuine advertisements. No relay address
                        # or transport PeerID is injected into either config.
                        server.visible_peer = None
                        await eventually(lambda: all(event.is_set() for event in ready),
                            detail=lambda: [r.discovery_status() for r in relays])
                        await eventually(lambda: all(n.ready.is_set() for n in nodes))
                        for index, relay in enumerate(relays):
                            other = relays[1 - index]
                            assert relay.peers == {other.identity: frozenset({servers[1 - index].expected_hash})}
                            assert impostor.get_id() not in relay.peers
                            assert different_group.get_id() not in relay.peers
                            assert bootstrap.get_id() not in relay.peers
                            assert relay.active
                            assert relay.discovery_status()["announcementsSent"] == 1
                            assert AMINO_PROTOCOL not in relay.host.get_mux().get_protocols()
                            proof = relay.proofs[servers[index].expected_hash]
                            verify_relay_proof(proof, selected, expected_peer_id=str(relay.identity))
                        assert not server.pubsub_streams
                        assert not wrong_group_requests
                        assert {request["profile"] for request in native_requests} == {"syn.private-pubsub-link/2"}

                        # The actual native endpoints encrypt, sign, acknowledge,
                        # decrypt, and sign receive receipts in both directions.
                        for sender, recipient in ((0, 1), (1, 0)):
                            payload = {"private_marker": secrets.token_hex(24), "direction": sender}
                            accepted = await trio.to_thread.run_sync(partial(control, nodes[sender], "publish",
                                {"topic": "syn://type/group-provider-test", "value": payload}))
                            assert accepted[0]["result"]["relay_accepted"]
                            assert not accepted[0]["result"]["peer_reception_proved"]
                            await eventually(lambda: bool(nodes[recipient].messages), timeout=5)
                            received = await trio.to_thread.run_sync(control, nodes[recipient], "receive")
                            entry, = received[0]["result"]["messages"]
                            assert entry["payload"] == payload
                            verify_message(entry["envelope"], selected,
                                expected_node=servers[sender].expected_hash,
                                expected_boot=servers[sender].boot_id)
                            assert payload["private_marker"].encode() not in canonical(forwarded)
                        assert not any(node.fault.is_set() for node in nodes)
                        assert not native_errors
                        relay_ids = {relay.identity for relay in relays}
                        assert {(record_key, peer) for record_key, peer in server.announcements
                                if peer in relay_ids} == {(key, peer) for peer in relay_ids}
                        assert all(record_key == key for record_key, peer in server.lookups if peer in relay_ids)
                        assert not server.pubsub_streams
                    finally:
                        for relay in relays:
                            relay.stopped.set()
                        with trio.fail_after(5):
                            for event in stopped:
                                await event.wait()
            finally:
                for node in nodes:
                    await trio.to_thread.run_sync(node.close)
                for sock in sockets:
                    sock.close()
                for worker in workers:
                    await trio.to_thread.run_sync(worker.join, 2)
                    assert not worker.is_alive()
    trio.run(exercise)
