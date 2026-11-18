"""Authenticated loopback discovery and real Bitswap public-key retrieval."""
from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from libp2p import create_yamux_muxer_option, new_host
from libp2p.bitswap.client import BitswapClient
from libp2p.bitswap.pb.bitswap_pb2 import Message
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.crypto.x25519 import create_new_key_pair as noise_key_pair
from libp2p.host.exceptions import StreamFailure
from libp2p.network.stream.exceptions import StreamReset
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.security.noise.transport import PROTOCOL_ID as NOISE, Transport
import multiaddr
import pytest
import trio

from syn import peer_record
from syn.peer_record_transport import (
    BITSWAP_PROTOCOLS, MAX_BITSWAP_BYTES, MAX_REQUEST_BYTES, MAX_WANTS,
    PEER_RECORD_PROTOCOL, RAW_CID_PREFIX, REQUEST_PROFILE,
    PeerRecordService, fetch_peer_record, raw_public_key_cid, read_frame, write_frame,
)


def host():
    keys = create_new_key_pair()
    return new_host(key_pair=keys, bootstrap=[], enable_upnp=False,
        sec_opt={NOISE: Transport(keys, noise_privkey=noise_key_pair().private_key)},
        muxer_opt=create_yamux_muxer_option()), keys


def packet(server, keys, operator_key, *, expires=None):
    public = operator_key.public_key().public_bytes_raw()
    now = int(time.time())
    body = {"profile": peer_record.PROFILE,
        "operator": hashlib.sha256(public).hexdigest(), "network_id": "syn",
        "peer_id": str(server.get_id()),
        "transport_public_key": keys.public_key.to_bytes().hex(),
        "addresses": sorted(str(a) for a in server.get_addrs()),
        "issued_at": now - 1 if expires is None else expires - 100,
        "expires_at": now + 300 if expires is None else expires}
    return {"body": body, "public_key": public.hex(),
        "signature": operator_key.sign(peer_record.DOMAIN + peer_record.canonical(body)).hex()}


def test_signed_record_round_trip_over_noise_and_yamux():
    async def exercise():
        server, keys = host()
        client, _ = host()
        operator_key = Ed25519PrivateKey.generate()
        public = operator_key.public_key().public_bytes_raw()
        current = None
        service = PeerRecordService(server, public, lambda: current)
        service.register()
        addresses = [multiaddr.Multiaddr("/ip4/127.0.0.1/tcp/0")]
        async with server.run(listen_addrs=addresses), client.run(listen_addrs=addresses):
            current = packet(server, keys, operator_key)
            result = await fetch_peer_record(client, info_from_p2p_addr(server.get_addrs()[0]),
                expected_operator=hashlib.sha256(public).hexdigest(), expected_network="syn")
            assert result == current
            assert result["body"]["peer_id"] == str(server.get_id())
            assert "/ipfs/kad/1.0.0" not in server.get_mux().get_protocols()
    trio.run(exercise)


@pytest.mark.parametrize("failure", ["signature", "network", "transport", "expired"])
def test_forged_or_stale_candidate_record_is_rejected(failure):
    async def exercise():
        server, keys = host()
        client, _ = host()
        other_host, other_keys = host()
        operator_key = Ed25519PrivateKey.generate()
        public = operator_key.public_key().public_bytes_raw()
        addresses = [multiaddr.Multiaddr("/ip4/127.0.0.1/tcp/0")]
        async with server.run(listen_addrs=addresses), client.run(listen_addrs=addresses):
            candidate = packet(server, keys, operator_key)
            if failure == "signature":
                candidate["signature"] = "00" * 64
            else:
                if failure == "network":
                    candidate["body"]["network_id"] = "other"
                elif failure == "transport":
                    candidate["body"]["peer_id"] = str(other_host.get_id())
                    candidate["body"]["transport_public_key"] = other_keys.public_key.to_bytes().hex()
                    candidate["body"]["addresses"] = ["/ip4/127.0.0.1/tcp/4001"]
                elif failure == "expired":
                    candidate = packet(server, keys, operator_key, expires=int(time.time()) - 30)
                candidate["signature"] = operator_key.sign(
                    peer_record.DOMAIN + peer_record.canonical(candidate["body"])).hex()

            async def malicious(stream):
                try:
                    await read_frame(stream, MAX_REQUEST_BYTES)
                    await write_frame(stream, peer_record.canonical(candidate), peer_record.MAX_RECORD_BYTES)
                finally:
                    await stream.close()
            server.set_stream_handler(PEER_RECORD_PROTOCOL, malicious)
            with pytest.raises(ValueError):
                await fetch_peer_record(client, info_from_p2p_addr(server.get_addrs()[0]),
                    expected_operator=hashlib.sha256(public).hexdigest(), expected_network="syn")
    trio.run(exercise)


@pytest.mark.parametrize("available", [False, True])
def test_server_refuses_absent_or_expired_record(available):
    async def exercise():
        server, keys = host()
        client, _ = host()
        operator_key = Ed25519PrivateKey.generate()
        public = operator_key.public_key().public_bytes_raw()
        current = None
        service = PeerRecordService(server, public, lambda: current)
        service.register()
        addresses = [multiaddr.Multiaddr("/ip4/127.0.0.1/tcp/0")]
        async with server.run(listen_addrs=addresses), client.run(listen_addrs=addresses):
            if available:
                current = packet(server, keys, operator_key, expires=int(time.time()) - 30)
            with pytest.raises((StreamReset, EOFError)):
                await fetch_peer_record(client, info_from_p2p_addr(server.get_addrs()[0]),
                    expected_operator=hashlib.sha256(public).hexdigest(), expected_network="syn")
            current = packet(server, keys, operator_key)
            assert await fetch_peer_record(client, info_from_p2p_addr(server.get_addrs()[0]),
                expected_operator=hashlib.sha256(public).hexdigest(), expected_network="syn") == current
    trio.run(exercise)


@pytest.mark.parametrize("raw_request", [
    b'{"operator":"' + b"0" * 64 + b'","profile":"' + REQUEST_PROFILE.encode() + b'"}',
    b'{"operator":"' + b"0" * 64 + b'","operator":"' + b"0" * 64 + b'","profile":"' + REQUEST_PROFILE.encode() + b'"}',
    b'{"operator":true,"profile":"' + REQUEST_PROFILE.encode() + b'"}',
])
def test_bad_requests_cannot_read_record_or_damage_host(raw_request):
    async def exercise():
        server, keys = host()
        client, _ = host()
        operator_key = Ed25519PrivateKey.generate()
        public = operator_key.public_key().public_bytes_raw()
        calls = []
        current = None
        def current_record():
            calls.append(True)
            return current
        service = PeerRecordService(server, public, current_record)
        service.register()
        addresses = [multiaddr.Multiaddr("/ip4/127.0.0.1/tcp/0")]
        async with server.run(listen_addrs=addresses), client.run(listen_addrs=addresses):
            current = packet(server, keys, operator_key)
            info = info_from_p2p_addr(server.get_addrs()[0])
            await client.connect(info)
            stream = await client.new_stream(server.get_id(), [PEER_RECORD_PROTOCOL])
            await write_frame(stream, raw_request, MAX_REQUEST_BYTES)
            with pytest.raises((StreamReset, EOFError)):
                with trio.fail_after(1):
                    await read_frame(stream, peer_record.MAX_RECORD_BYTES)
            assert not calls
            assert await fetch_peer_record(client, info,
                expected_operator=hashlib.sha256(public).hexdigest(), expected_network="syn") == current
    trio.run(exercise)


def test_slow_request_and_inbound_limit_release_capacity():
    async def exercise():
        server, keys = host()
        client, _ = host()
        operator_key = Ed25519PrivateKey.generate()
        public = operator_key.public_key().public_bytes_raw()
        current = None
        service = PeerRecordService(server, public, lambda: current, timeout=0.3, max_inbound=1)
        service.register()
        addresses = [multiaddr.Multiaddr("/ip4/127.0.0.1/tcp/0")]
        async with server.run(listen_addrs=addresses), client.run(listen_addrs=addresses):
            current = packet(server, keys, operator_key)
            info = info_from_p2p_addr(server.get_addrs()[0])
            await client.connect(info)
            slow = await client.new_stream(server.get_id(), [PEER_RECORD_PROTOCOL])
            await slow.write(b"\x80")
            with trio.fail_after(1):
                while service._slots.value:
                    await trio.sleep(0.001)
            with pytest.raises((StreamFailure, StreamReset, EOFError)):
                await fetch_peer_record(client, info, expected_operator=hashlib.sha256(public).hexdigest(),
                    expected_network="syn", timeout=1)
            with pytest.raises((StreamReset, EOFError)):
                with trio.fail_after(1):
                    await read_frame(slow, peer_record.MAX_RECORD_BYTES)
            with trio.fail_after(1):
                while service._slots.value != 1:
                    await trio.sleep(0.001)
            assert await fetch_peer_record(client, info,
                expected_operator=hashlib.sha256(public).hexdigest(), expected_network="syn") == current
    trio.run(exercise)


def test_upstream_bitswap_client_retrieves_exact_public_key_block():
    async def exercise():
        server, _ = host()
        client, _ = host()
        public = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        service = PeerRecordService(server, public, lambda: None)
        service.register()
        receiver = BitswapClient(client)
        await receiver.start()
        addresses = [multiaddr.Multiaddr("/ip4/127.0.0.1/tcp/0")]
        async with server.run(listen_addrs=addresses), client.run(listen_addrs=addresses):
            await client.connect(info_from_p2p_addr(server.get_addrs()[0]))
            async with trio.open_nursery() as nursery:
                receiver.set_nursery(nursery)
                actual = await receiver.get_block(service.cid, peer_id=server.get_id(), timeout=5)
                assert actual == public
                assert len(actual) == 32
                assert service.cid == RAW_CID_PREFIX + hashlib.sha256(actual).digest()
                nursery.cancel_scope.cancel()
        await receiver.stop()
    trio.run(exercise)


@pytest.mark.parametrize("protocol", BITSWAP_PROTOCOLS)
def test_bitswap_reverse_stream_and_message_semantics(protocol):
    async def exercise():
        server, _ = host()
        client, _ = host()
        public = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        service = PeerRecordService(server, public, lambda: None)
        service.register()
        got = trio.Event()
        messages = []
        async def receive(stream):
            try:
                response = Message()
                response.ParseFromString(await read_frame(stream, MAX_BITSWAP_BYTES))
                messages.append(response)
                assert stream.muxed_conn.peer_id == server.get_id()
                got.set()
            finally:
                await stream.close()
        client.set_stream_handler(protocol, receive)
        addresses = [multiaddr.Multiaddr("/ip4/127.0.0.1/tcp/0")]
        async with server.run(listen_addrs=addresses), client.run(listen_addrs=addresses):
            await client.connect(info_from_p2p_addr(server.get_addrs()[0]))
            request = Message()
            request.wantlist.entries.add(block=service.cid, priority=10,
                wantType=Message.Wantlist.Have)
            missing = raw_public_key_cid(b"x" * 32)
            request.wantlist.entries.add(block=missing, sendDontHave=True)
            stream = await client.new_stream(server.get_id(), [protocol])
            await write_frame(stream, request.SerializeToString(), MAX_BITSWAP_BYTES)
            with trio.fail_after(5):
                await got.wait()
            await stream.close()
            if protocol == BITSWAP_PROTOCOLS[0]:
                assert not messages[0].payload
                assert [(p.cid, p.type) for p in messages[0].blockPresences] == [
                    (service.cid, Message.Have), (missing, Message.DontHave)]
            else:
                assert not messages[0].blockPresences
                assert messages[0].payload[0].data == public
    trio.run(exercise)


def test_bitswap_cancel_and_foreign_blocks_do_not_become_available():
    public = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    service = PeerRecordService(SimpleNamespace(), public, lambda: None)
    foreign = raw_public_key_cid(b"x" * 32)
    message = Message()
    message.wantlist.entries.add(block=service.cid)
    message.wantlist.entries.add(block=service.cid, cancel=True)
    message.wantlist.entries.add(block=foreign, sendDontHave=True)
    message.payload.add(prefix=RAW_CID_PREFIX, data=b"x" * 32)
    response = service._bitswap_response(message.SerializeToString(), BITSWAP_PROTOCOLS[0])
    assert not response.payload
    assert len(response.blockPresences) == 1
    assert response.blockPresences[0].type == Message.DontHave
    message.ClearField("payload")
    response = service._bitswap_response(message.SerializeToString(), BITSWAP_PROTOCOLS[0])
    assert response.blockPresences[0].type == Message.DontHave


class MemoryStream:
    def __init__(self, data):
        self.data = bytearray(data)
        self.reads = []
    async def read(self, count):
        self.reads.append(count)
        chunk = bytes(self.data[:count])
        del self.data[:count]
        return chunk


@pytest.mark.parametrize("frame", [b"\x80\x00", b"\x80" * 5, b"\x81\x02", b"\x04abc"])
def test_bounded_framing_rejects_malformed_or_oversized_payloads(frame):
    async def exercise():
        stream = MemoryStream(frame)
        with pytest.raises((ValueError, EOFError)):
            await read_frame(stream, 256)
        assert max(stream.reads) <= 256
    trio.run(exercise)


def test_wantlist_bound_applies_before_processing():
    service = PeerRecordService(SimpleNamespace(), b"a" * 32, lambda: None)
    message = Message()
    for _ in range(MAX_WANTS + 1):
        message.wantlist.entries.add(block=service.cid)
    with pytest.raises(ValueError, match="capacity"):
        service._bitswap_response(message.SerializeToString(), BITSWAP_PROTOCOLS[0])
