"""Real managed hosts discover open Syn URL groups through DHT providers."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import secrets
import threading

from libp2p.kad_dht.pb.kademlia_pb2 import Message
from libp2p.peer.id import ID
from multiaddr import Multiaddr
import pytest
import trio
import varint

from syn.amino import AminoDHTClient
from syn.config import P2PConfig
from syn.p2p import Libp2pNode
from syn.p2p_defaults import AMINO_PROTOCOL
from syn_client.operator_protocol import canonical

from test_p2p_amino_runtime import read_dht_request, secure_host


class GroupProviderDHT:
    """Independent wire fixture retaining every advertised provider per key."""

    def __init__(self, host):
        self.records = {}
        self.announcements = []
        self.lookups = []
        host.set_stream_handler(AMINO_PROTOCOL, self.handle)

    async def handle(self, stream):
        try:
            with trio.fail_after(3):
                request = await read_dht_request(stream)
                remote = stream.muxed_conn.peer_id
                if request.type == Message.ADD_PROVIDER:
                    assert request.key[:2] == b"\x12\x20" and len(request.key) == 34
                    assert len(request.providerPeers) == 1
                    provider = request.providerPeers[0]
                    assert provider.id == remote.to_bytes()
                    retained = Message.Peer()
                    retained.CopyFrom(provider)
                    self.records.setdefault(request.key, {})[provider.id] = retained
                    self.announcements.append((request.key, str(remote)))
                    return  # Public ADD_PROVIDER has no storage acknowledgement.
                assert request.type in (Message.FIND_NODE, Message.GET_PROVIDERS)
                response = Message(type=request.type)
                if request.type == Message.GET_PROVIDERS:
                    self.lookups.append((request.key, str(remote)))
                    for provider in self.records.get(request.key, {}).values():
                        response.providerPeers.add().CopyFrom(provider)
                payload = response.SerializeToString()
                await stream.write(varint.encode(len(payload)) + payload)
        finally:
            await stream.close()


def expected_group(network, url):
    digest = hashlib.sha256(canonical({"profile": "syn.public-gossipsub-group/1",
                                      "network_id": network, "url": url})).digest()
    return digest.hex(), b"\x12\x20" + digest


def lab_config(seed):
    return P2PConfig(
        listenAddresses=("/ip4/127.0.0.1/tcp/0",),
        bootstrapPeers=(seed,), allowPrivateAddresses=True,
        mdnsEnabled=False, upnpEnabled=False,
        startupTimeoutSeconds=10, requestTimeoutSeconds=2,
        lookupTimeoutSeconds=5, bootstrapRefreshSeconds=60,
        publicPubsubDiscoveryIntervalSeconds=1,
        publicPubsubProviderRepublishSeconds=60,
        publicPubsubQueueSize=16,
    )


async def eventually(action, predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while True:
            value = await action()
            if predicate(value):
                return value
            await asyncio.sleep(0.02)


async def group_status(node, group_hash):
    status = await node.public_groups()
    return next(group for group in status["groups"] if group["group_hash"] == group_hash)


async def wait_mesh(node, group_hash, minimum=1):
    return await eventually(lambda: group_status(node, group_hash),
                            lambda group: group["mesh_peer_count"] >= minimum)


async def receive_data(node, group_hash, data):
    result = await eventually(lambda: node.read_public(group_hash),
                             lambda page: any(message["data"] == data for message in page["messages"]))
    return next(message for message in result["messages"] if message["data"] == data)


def test_open_url_groups_discover_independent_peers_and_preserve_namespace_isolation(monkeypatch):
    # Observe and independently retain the real libp2p signature check. This
    # wrapper does not replace cryptography or inject peer addresses.
    pubsub_module = importlib.import_module("libp2p.pubsub.pubsub")
    validate_signature = pubsub_module.signature_validator
    signed_packets = []

    def record_signature(message):
        valid = validate_signature(message)
        if valid:
            signed_packets.append((str(ID(message.from_id)), bytes(message.data),
                                   tuple(message.topicIDs), bytes(message.signature)))
        return valid

    monkeypatch.setattr(pubsub_module, "signature_validator", record_signature)
    groups = [("syn", f"syn://{kind}/public-room") for kind in ("type", "cog", "ember", "event")]
    groups.append(("syn-other", "syn://type/public-room"))
    expected = {entry: expected_group(*entry) for entry in groups}
    assert len({value[0] for value in expected.values()}) == 5

    async def exercise():
        bootstrap = secure_host()
        dht = GroupProviderDHT(bootstrap)
        main_token = trio.lowlevel.current_trio_token()

        async def managed(seed):
            # No Operator, Agent certificate, fixed cohort, or database object
            # is required for any of these independent public participants.
            first, second, late = [Libp2pNode(lab_config(seed)) for _ in range(3)]
            nodes = [first, second, late]
            try:
                await first.start()
                await second.start()
                for network, url in groups:
                    joined = await first.join_public_group(url, network_id=network)
                    alias = await second.join_public_group(url.replace("syn://", "synny://"),
                                                           network_id=network)
                    digest, key = expected[(network, url)]
                    assert joined["group_hash"] == alias["group_hash"] == digest
                    assert joined["provider_key"] == alias["provider_key"] == key.hex()
                    assert joined["url"] == alias["url"] == url
                    assert joined["topic"] == "/syn/public-pubsub/1/" + digest
                    assert joined["joined"] and alias["joined"]

                def advertised():
                    return all(len(dht.records.get(key, {})) >= 2 for _, key in expected.values())

                async def advertisements():
                    return trio.from_thread.run_sync(advertised, trio_token=main_token)

                await eventually(advertisements, bool)
                for digest, _ in expected.values():
                    await wait_mesh(first, digest)
                    await wait_mesh(second, digest)

                # Each object kind and network has its own topic even though
                # the hosts share the same underlying libp2p connection.
                payloads = {}
                for network, url in groups:
                    digest, _ = expected[(network, url)]
                    payload = {"marker": secrets.token_hex(16), "url": url, "network": network}
                    payloads[digest] = payload
                    published = await first.publish_public(digest, payload)
                    assert published["accepted"] is True
                    assert published["peer_reception_proved"] is False
                    received = await receive_data(second, digest, payload)
                    assert received["sender_peer_id"] == first.status()["peerId"]
                    assert received["message_id"] == published["message_id"]

                for digest, payload in payloads.items():
                    page = await second.read_public(digest)
                    assert [message["data"] for message in page["messages"]] == [payload]
                    # Reads are cursor-based and nondestructive.
                    assert await second.read_public(digest) == page

                digest, key = expected[groups[0]]
                old_page = await second.read_public(digest)
                old_cursor = old_page["cursor"]
                await late.start()
                late_group = await late.join_public_group("synny://type/public-room")
                assert late_group["group_hash"] == digest
                await wait_mesh(late, digest)
                await wait_mesh(first, digest, minimum=2)
                late_payload = {"marker": secrets.token_hex(16), "late_participant": True}
                await late.publish_public(digest, late_payload)
                assert (await receive_data(first, digest, late_payload))["sender_peer_id"] == late.status()["peerId"]
                assert (await receive_data(second, digest, late_payload))["sender_peer_id"] == late.status()["peerId"]

                # Leaving one topic does not stop other subscriptions or the
                # host. Rejoining creates a fresh receive-stream generation.
                left = await second.leave_public_group(digest)
                assert left["joined"] is False
                assert digest not in {group["group_hash"] for group in (await second.public_groups())["groups"]}
                assert len((await second.public_groups())["groups"]) == 4
                assert second.status()["running"]
                rejoined = await second.join_public_group(groups[0][1])
                assert rejoined["stream_id"] != old_page["stream_id"]
                from syn.public_pubsub import PublicCursorExpired
                with pytest.raises(PublicCursorExpired):
                    await second.read_public(digest, after=old_cursor)
                after_rejoin = {"marker": secrets.token_hex(16), "new_generation": True}
                accepted = await second.publish_public(digest, after_rejoin)
                assert accepted["accepted"] and not accepted["peer_reception_proved"]
                own_message = await receive_data(second, digest, after_rejoin)
                assert own_message["cursor"].startswith(rejoined["stream_id"] + ":")
                # Remote PRUNE backoff can delay immediate mesh reattachment.
                # Other joined groups continue delivering over this host.
                other_digest, _ = expected[groups[1]]
                other_payload = {"marker": secrets.token_hex(16), "unaffected_group": True}
                await second.publish_public(other_digest, other_payload)
                await receive_data(first, other_digest, other_payload)

                for node in nodes:
                    node.check()
                    assert node.operator is None
                    assert AMINO_PROTOCOL not in node._host.get_mux().get_protocols()
                assert any(sender == first.status()["peerId"]
                           and payloads[digest]["marker"].encode() in raw
                           and topics == ("/syn/public-pubsub/1/" + digest,) and signature
                           for sender, raw, topics, signature in signed_packets)
                # Plaintext is visible to public topic participants inside the
                # signed GossipSub envelope; Noise protects transport hops.
            finally:
                stopped = await asyncio.gather(*(node.stop() for node in nodes), return_exceptions=True)
                for result in stopped:
                    if isinstance(result, BaseException):
                        raise result
                assert all(not node.status()["running"] for node in nodes)

        async with bootstrap.run(listen_addrs=[Multiaddr("/ip4/127.0.0.1/tcp/0")]):
            with trio.fail_after(45):
                await trio.to_thread.run_sync(lambda: asyncio.run(managed(str(bootstrap.get_addrs()[0]))))
        keys = {value[1] for value in expected.values()}
        assert {key for key, _ in dht.announcements} == keys
        assert {key for key, _ in dht.lookups} == keys
        assert len(dht.records[expected[groups[0]][1]]) == 3

    trio.run(exercise)


def test_managed_shutdown_cancels_inflight_public_provider_lookup(monkeypatch):
    entered, cancelled = threading.Event(), threading.Event()

    async def blocked_lookup(self, key, count=20):
        entered.set()
        try:
            await trio.sleep_forever()
        finally:
            cancelled.set()

    monkeypatch.setattr(AminoDHTClient, "find_providers", blocked_lookup)

    async def exercise():
        bootstrap = secure_host()
        GroupProviderDHT(bootstrap)

        async def managed(seed):
            node = Libp2pNode(lab_config(seed))
            try:
                await node.start()
                await node.join_public_group("syn://event/shutdown-fixture")
                assert await asyncio.to_thread(entered.wait, 5)
                async with asyncio.timeout(3):
                    await node.stop()
                assert cancelled.is_set()
                assert not node.status()["running"]
                assert node._error is None
            finally:
                await node.stop()

        async with bootstrap.run(listen_addrs=[Multiaddr("/ip4/127.0.0.1/tcp/0")]):
            with trio.fail_after(15):
                await trio.to_thread.run_sync(lambda: asyncio.run(managed(str(bootstrap.get_addrs()[0]))))

    trio.run(exercise)
