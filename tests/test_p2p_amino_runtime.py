"""Managed asyncio/Trio runtime, real encrypted discovery, and identity binding."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import hashlib
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.crypto.x25519 import create_new_key_pair as noise_key_pair
from libp2p.host.exceptions import StreamFailure
from libp2p.kad_dht.pb.kademlia_pb2 import Message
from libp2p.peer.id import ID
from libp2p.peer.peerinfo import PeerInfo, info_from_p2p_addr
from libp2p.security.noise.transport import Transport as NoiseTransport
from libp2p.stream_muxer.yamux.yamux import Yamux
from multiaddr import Multiaddr
import pytest
import trio
import varint

from syn.config import P2PConfig
from syn import peer_record
from syn.p2p import Libp2pNode
from syn.p2p_defaults import AMINO_PROTOCOL
from syn.peer_record_transport import (
    MAX_REQUEST_BYTES, PEER_RECORD_PROTOCOL, read_frame, write_frame,
)


class SyntheticOperatorSigner:
    """A test-only base key; no installed Operator or host custody is used."""

    def __init__(self):
        self.key = Ed25519PrivateKey.from_private_bytes(b"m" * 32)
        self.public_key = self.key.public_key().public_bytes_raw()
        self.signed = []

    def sign(self, payload):
        self.signed.append(payload)
        return self.key.sign(payload)


def secure_host():
    identity = create_new_key_pair()
    return new_host(
        key_pair=identity, bootstrap=[], enable_upnp=False,
        sec_opt={"/noise": NoiseTransport(identity, noise_key_pair().private_key)},
        muxer_opt={"/yamux/1.0.0": Yamux},
    )


async def read_dht_request(stream):
    # Independent test-server framing, rather than the client decoder under test.
    prefix = bytearray()
    for _ in range(5):
        byte = await stream.read(1)
        if not byte:
            raise EOFError
        prefix.extend(byte)
        if not byte[0] & 128:
            break
    length = varint.decode_bytes(bytes(prefix))
    assert 0 < length <= 128 * 1024
    payload = bytearray()
    while len(payload) < length:
        chunk = await stream.read(length - len(payload))
        if not chunk:
            raise EOFError
        payload.extend(chunk)
    message = Message()
    message.ParseFromString(bytes(payload))
    return message


def test_managed_nodes_publish_resolve_and_reject_a_forged_provider():
    async def exercise():
        bootstrap, impostor = secure_host(), secure_host()
        main_token = trio.lowlevel.current_trio_token()
        retained = {}
        announcements = []
        lookups = []
        replay_requests = []
        attack = {"mode": "genuine", "packet": None}
        signer = SyntheticOperatorSigner()
        base_hash = hashlib.sha256(signer.public_key).hexdigest()
        expected_key = b"\x12\x20" + hashlib.sha256(signer.public_key).digest()
        operator = SimpleNamespace(
            signer=signer, expected_hash=base_hash, config={"network_id": "syn"},
        )

        async def dht_server(stream):
            try:
                with trio.fail_after(3):
                    request = await read_dht_request(stream)
                    if request.type == Message.ADD_PROVIDER:
                        # A provider announcement must name its authenticated
                        # sender, and use the raw base-key block's multihash.
                        assert request.key == expected_key
                        assert len(request.providerPeers) == 1
                        provider = request.providerPeers[0]
                        assert provider.id == stream.muxed_conn.peer_id.to_bytes()
                        retained[request.key] = Message.Peer()
                        retained[request.key].CopyFrom(provider)
                        announcements.append((request.key, provider.id))
                        return  # Kubo sends no ADD_PROVIDER acknowledgement.
                    assert request.type in (Message.FIND_NODE, Message.GET_PROVIDERS)
                    response = Message(type=request.type)
                    if request.type == Message.GET_PROVIDERS:
                        lookups.append(request.key)
                        if attack["mode"] in {"forged", "mixed"}:
                            rogue = info_from_p2p_addr(impostor.get_addrs()[0])
                            candidate = response.providerPeers.add()
                            candidate.id = rogue.peer_id.to_bytes()
                            candidate.addrs.extend(a.to_bytes() for a in rogue.addrs)
                        if attack["mode"] in {"genuine", "mixed"} and request.key in retained:
                            response.providerPeers.add().CopyFrom(retained[request.key])
                    encoded = response.SerializeToString()
                    await stream.write(varint.encode(len(encoded)) + encoded)
            finally:
                await stream.close()

        async def replay_another_peers_record(stream):
            try:
                with trio.fail_after(3):
                    await read_frame(stream, MAX_REQUEST_BYTES)
                    replay_requests.append(stream.muxed_conn.peer_id.to_bytes())
                    # This is a genuinely signed, current Operator record. It
                    # must still fail when served by a different Noise PeerID.
                    packet = attack["packet"]
                    assert packet is not None
                    await write_frame(stream, peer_record.canonical(packet), peer_record.MAX_RECORD_BYTES)
            finally:
                await stream.close()

        bootstrap.set_stream_handler(AMINO_PROTOCOL, dht_server)
        impostor.set_stream_handler(PEER_RECORD_PROTOCOL, replay_another_peers_record)

        def set_attack(mode, packet):
            attack.update(mode=mode, packet=packet)

        async def require_client_only(peer_text, addresses):
            remote = PeerInfo(ID.from_base58(peer_text), [Multiaddr(a) for a in addresses])
            with trio.fail_after(3):
                await bootstrap.connect(remote)
                with pytest.raises(StreamFailure):
                    await bootstrap.new_stream(remote.peer_id, [AMINO_PROTOCOL])

        async def managed_nodes(seed):
            config = P2PConfig(
                listenAddresses=("/ip4/127.0.0.1/tcp/0",),
                bootstrapPeers=(seed,), allowPrivateAddresses=True,
                mdnsEnabled=False, upnpEnabled=False,
                startupTimeoutSeconds=10, requestTimeoutSeconds=2,
                lookupTimeoutSeconds=8, bootstrapRefreshSeconds=60,
                providerRepublishSeconds=60,
            )
            publisher, reader = Libp2pNode(config, operator), Libp2pNode(config)

            async def wait_for_ready_publication():
                async with asyncio.timeout(10):
                    while not (publisher.status()["publication"]["state"] == "announced"
                               and reader.status()["amino"]["state"] == "ready"
                               and trio.from_thread.run_sync(
                                   lambda: bool(announcements), trio_token=main_token)):
                        publisher.check()
                        reader.check()
                        await asyncio.sleep(0.02)

            try:
                await publisher.start()
                await reader.start()
                await wait_for_ready_publication()
                publication = publisher.status()["publication"]
                publisher_id = publisher.status()["peerId"]
                assert publication["providerKey"] == expected_key.hex()
                assert publication["operator"] == base_hash
                assert publication["announcedTo"] == 1
                assert reader.status()["publication"]["state"] == "no-operator"

                records = await asyncio.wait_for(reader.find_operator(base_hash), 10)
                assert len(records) == 1
                packet = records[0]
                body = peer_record.verify(packet, expected_operator=base_hash,
                    expected_network="syn", expected_peer_id=publisher_id)
                assert body["operator"] == base_hash
                assert body["peer_id"] == publisher_id
                assert packet["public_key"] == signer.public_key.hex()
                assert expected_key[2:] != hashlib.sha256(bytes.fromhex(body["transport_public_key"])).digest()
                assert signer.signed

                # Return only a real reachable impostor replaying the valid
                # publisher packet. No duplicate-result filtering can hide an
                # erroneous acceptance in this phase: the result must be empty.
                trio.from_thread.run_sync(set_attack, "forged", packet, trio_token=main_token)
                assert await asyncio.wait_for(reader.find_operator(base_hash), 10) == []
                trio.from_thread.run_sync(set_attack, "mixed", packet, trio_token=main_token)
                mixed = await asyncio.wait_for(reader.find_operator(base_hash), 10)
                assert [p["body"]["peer_id"] for p in mixed] == [publisher_id]

                for node in (publisher, reader):
                    status = node.status()
                    assert status["amino"]["mode"] == "client"
                    assert AMINO_PROTOCOL not in node._host.get_mux().get_protocols()
                    await asyncio.to_thread(trio.from_thread.run, require_client_only,
                        status["peerId"], status["listenAddresses"], trio_token=main_token)
            finally:
                stopped = await asyncio.gather(reader.stop(), publisher.stop(), return_exceptions=True)
                for result in stopped:
                    if isinstance(result, BaseException):
                        raise result
                assert not publisher.status()["running"]
                assert not reader.status()["running"]

        async with AsyncExitStack() as stack:
            for host in (bootstrap, impostor):
                await stack.enter_async_context(host.run(
                    listen_addrs=[Multiaddr("/ip4/127.0.0.1/tcp/0")]))
            seed = str(bootstrap.get_addrs()[0])
            with trio.fail_after(45):
                await trio.to_thread.run_sync(lambda: asyncio.run(managed_nodes(seed)))
            assert len(announcements) == 1
            assert announcements[0][0] == expected_key
            assert all(key == expected_key for key in lookups)
            assert len(lookups) >= 3
            assert len(replay_requests) == 2
    trio.run(exercise)
