from __future__ import annotations

from contextlib import AsyncExitStack
import hashlib
from types import SimpleNamespace

from multiaddr import Multiaddr
import pytest
import trio
import varint

from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.crypto.x25519 import create_new_key_pair as noise_keys
from libp2p.kad_dht.pb.kademlia_pb2 import Message
from libp2p.peer.id import ID
from libp2p.peer.peerinfo import PeerInfo, info_from_p2p_addr
from libp2p.peer.peerstore import PeerStore
from libp2p.security.noise.transport import Transport as NoiseTransport
from libp2p.stream_muxer.yamux.yamux import Yamux

from syn.amino import (
    AMINO_PROTOCOL, IPFS_BOOTSTRAP_PEERS, MAX_FRAME_BYTES, MAX_QUERIES,
    AminoDHTClient, _read_frame, dialable_tcp_addresses, seed_dial_addresses,
)


def peer(value=1):
    return ID(b"\x12\x20" + hashlib.sha256(str(value).encode()).digest())


def secure_host():
    identity = create_new_key_pair()
    return new_host(key_pair=identity, bootstrap=[], enable_upnp=False,
                    sec_opt={"/noise": NoiseTransport(identity, noise_keys().private_key)},
                    muxer_opt={"/yamux/1.0.0": Yamux})


def info(host):
    return info_from_p2p_addr(host.get_addrs()[0])


def add_peer(message, field, value):
    item = getattr(message, field).add()
    item.id = value.peer_id.to_bytes()
    item.addrs.extend(address.to_bytes() for address in value.addrs)


async def read_server_message(stream):
    """Independent tiny test-server decoder for real encrypted RPC exchanges."""
    prefix = bytearray()
    for _ in range(5):
        byte = await stream.read(1)
        if not byte:
            raise EOFError
        prefix.extend(byte)
        if not byte[0] & 128:
            break
    remaining = varint.decode_bytes(bytes(prefix))
    data = bytearray()
    while remaining:
        chunk = await stream.read(remaining)
        if not chunk:
            raise EOFError
        data.extend(chunk)
        remaining -= len(chunk)
    result = Message()
    result.ParseFromString(bytes(data))
    return result


def install_server(host, respond):
    async def handler(stream):
        try:
            request = await read_server_message(stream)
            response = respond(request, stream.muxed_conn.peer_id)
            if response is not None:
                data = response.SerializeToString()
                # Deliberately fragment frames: real readers must not assume
                # one stream.read(n) returns all n bytes.
                wire = varint.encode(len(data)) + data
                for offset in range(0, len(wire), 7):
                    await stream.write(wire[offset:offset + 7])
        finally:
            await stream.close()
    host.set_stream_handler(AMINO_PROTOCOL, handler)


def test_bootstrap_uses_five_official_pinned_dnsaddr_peers():
    assert len(IPFS_BOOTSTRAP_PEERS) == 5
    for value in IPFS_BOOTSTRAP_PEERS:
        address = Multiaddr(value)
        assert [p.name for p in address.protocols()] == ["dnsaddr", "p2p"]
        assert address.value_for_protocol("dnsaddr").endswith(".bootstrap.libp2p.io")
        assert info_from_p2p_addr(address).peer_id


def test_public_address_filter_preserves_transport_peer_pin(monkeypatch):
    own, other = peer(1), peer(2)
    addresses = [
        Multiaddr("/ip4/0.0.0.0/tcp/4001"),
        Multiaddr("/ip4/127.0.0.1/tcp/4001"),
        Multiaddr("/ip4/10.0.0.1/tcp/4001"),
        Multiaddr("/ip4/224.0.0.1/tcp/4001"),
        Multiaddr("/ip4/8.8.8.8/tcp/0"),
        Multiaddr(f"/ip4/8.8.8.8/tcp/4001/p2p/{other}"),
        Multiaddr(f"/ip4/8.8.8.8/tcp/4001/p2p/{own}"),
        Multiaddr("/ip4/8.8.8.8/udp/4001/quic-v1"),
        Multiaddr("/ip6/2001:4860:4860::8888/tcp/4001"),
    ]
    monkeypatch.setattr("syn.amino.OUTBOUND_FAMILIES.ipv6", lambda: True)
    public = dialable_tcp_addresses(addresses, peer_id=own)
    assert [str(a) for a in public] == [
        "/ip4/8.8.8.8/tcp/4001", "/ip6/2001:4860:4860::8888/tcp/4001"]
    lab = dialable_tcp_addresses(addresses, peer_id=own, allow_private=True)
    assert Multiaddr("/ip4/127.0.0.1/tcp/4001") in lab
    assert all("0.0.0.0" not in str(a) and "224.0.0.1" not in str(a) for a in lab)


class BytesStream:
    def __init__(self, data=b"", *, wait=False):
        self.data, self.wait = data, wait
        self.writes = []
        self.closed = self.reset_called = False
        self.request_sizes = []

    async def read(self, size):
        self.request_sizes.append(size)
        if self.wait:
            await trio.sleep_forever()
        take = min(size, 3, len(self.data))
        result, self.data = self.data[:take], self.data[take:]
        return result

    async def write(self, value):
        self.writes.append(value)

    async def close(self):
        self.closed = True

    async def reset(self):
        self.reset_called = True


class FakeHost:
    def __init__(self, stream=None):
        self.stream = stream
        self.store = SimpleNamespace(add_addrs=lambda *args: None, clear_addrs=lambda *args: None)

    def get_id(self):
        return peer(0)

    def get_peerstore(self):
        return self.store

    async def new_stream(self, remote, protocols):
        assert protocols == [AMINO_PROTOCOL]
        return self.stream


def test_peerstore_is_reseeded_before_a_dht_dial():
    async def exercise():
        remote = peer(1)
        private = Multiaddr("/ip4/127.0.0.1/tcp/4001")
        public = Multiaddr("/ip4/8.8.8.8/tcp/4001")
        response = Message(type=Message.FIND_NODE).SerializeToString()
        host = FakeHost(BytesStream(varint.encode(len(response)) + response))
        host.store = PeerStore()
        host.store.add_addrs(remote, [private], 600)
        original = host.new_stream

        async def check_before_dial(peer_id, protocols):
            assert host.store.addrs(peer_id) == [public]
            return await original(peer_id, protocols)

        host.new_stream = check_before_dial
        client = AminoDHTClient(host, [])
        assert await client._query(PeerInfo(remote, [public, private]), Message.FIND_NODE, b"target") is not None
        with pytest.raises(ValueError, match="permitted concrete"):
            seed_dial_addresses(host, PeerInfo(remote, [private]))
        # Invalid candidates do not erase an existing usable public route.
        assert host.store.addrs(remote) == [public]
    trio.run(exercise)


@pytest.mark.parametrize("wire", [
    b"\x80" * 5,
    b"\x81\x00\x00",  # nonminimal varint
    varint.encode(MAX_FRAME_BYTES + 1),
    varint.encode(5) + b"xx",  # truncated body
    b"\x00",  # no protobuf body
])
def test_malformed_and_oversized_frames_are_rejected_without_large_read(wire):
    async def exercise():
        stream = BytesStream(wire)
        with pytest.raises(ValueError):
            await _read_frame(stream)
        assert max(stream.request_sizes) <= MAX_FRAME_BYTES
    trio.run(exercise)


def test_rpc_timeout_resets_stream_and_external_cancellation_propagates():
    async def exercise():
        remote = PeerInfo(peer(1), [Multiaddr("/ip4/127.0.0.1/tcp/4001")])
        first = BytesStream(wait=True)
        client = AminoDHTClient(FakeHost(first), [], timeout=0.02, allow_private=True)
        assert await client._query(remote, Message.FIND_NODE, b"target") is None
        assert first.reset_called and client.status()["queryFailures"] == 1

        second = BytesStream(wait=True)
        client = AminoDHTClient(FakeHost(second), [], timeout=10, allow_private=True)
        with trio.move_on_after(0.02) as cancelled:
            await client._query(remote, Message.FIND_NODE, b"target")
        assert cancelled.cancelled_caught
        assert second.reset_called
        assert client.status()["queryFailures"] == 0
    trio.run(exercise)


def test_response_type_key_and_peer_bounds_are_checked():
    async def exercise():
        remote = PeerInfo(peer(), [Multiaddr("/ip4/8.8.8.8/tcp/4001")])
        packets = [Message(type=Message.GET_PROVIDERS, key=b"target"),
                   Message(type=Message.FIND_NODE, key=b"different")]
        oversized = Message(type=Message.FIND_NODE)
        for _ in range(65):
            oversized.closerPeers.add().id = peer().to_bytes()
        packets.append(oversized)
        for packet in packets:
            data = packet.SerializeToString()
            stream = BytesStream(varint.encode(len(data)) + data)
            client = AminoDHTClient(FakeHost(stream), [])
            assert await client._query(remote, Message.FIND_NODE, b"target") is None
            assert stream.reset_called
            assert client.status()["routingPeers"] == 0
    trio.run(exercise)


def test_dnsaddr_is_bounded_and_cannot_replace_pinned_peer_or_use_private_ip():
    async def exercise():
        own, other = peer(1), peer(2)
        calls = []
        client = AminoDHTClient(FakeHost(), [])

        async def records(name, kind):
            calls.append((name, kind))
            assert kind == "TXT"
            if name == "_dnsaddr.seed.example":
                values = [f"/dnsaddr/nested.example/p2p/{own}",
                          f"/ip4/8.8.4.4/tcp/4001/p2p/{other}",
                          f"/ip4/10.0.0.1/tcp/4001/p2p/{own}"]
            else:
                values = [f"/ip4/8.8.8.8/tcp/4001/p2p/{own}",
                          f"/dnsaddr/seed.example/p2p/{own}"]
            return [SimpleNamespace(strings=[("dnsaddr=" + x).encode()]) for x in values]

        client._dns_records = records
        result = await client._resolve_bootstrap(f"/dnsaddr/seed.example/p2p/{own}")
        assert result.peer_id == own
        assert [str(a) for a in result.addrs] == ["/ip4/8.8.8.8/tcp/4001"]
        assert len(calls) == 2
    trio.run(exercise)


def test_binary_multihash_required_and_private_addresses_not_announced():
    async def exercise():
        client = AminoDHTClient(FakeHost(), [])
        for invalid in ("ab" * 32, b"x" * 32, ("12" + "20" + "ab" * 32).encode()):
            with pytest.raises(ValueError, match="binary SHA2-256"):
                await client.provide(invalid, [])
            with pytest.raises(ValueError, match="binary SHA2-256"):
                await client.find_providers(invalid)
        key = b"\x12\x20" + hashlib.sha256(b"public key").digest()
        assert await client.provide(key, [Multiaddr("/ip4/127.0.0.1/tcp/4001")]) == 0
        assert client.status()["announcementStatus"] == "no-public-addresses"
    trio.run(exercise)


def test_lookup_stops_at_query_budget_when_peers_keep_supplying_new_candidates():
    async def exercise():
        client = AminoDHTClient(FakeHost(), [], allow_private=True)
        client._remember(PeerInfo(peer(1), [Multiaddr("/ip4/127.0.0.1/tcp/4001")]))
        calls = []
        ranks = {peer(1).to_bytes(): -1}
        # Each newly revealed peer is closer than every previous candidate.
        # This adversarial overlay would otherwise keep this lookup running.
        client._distance = lambda remote, target: ranks[remote.peer_id.to_bytes()]

        async def query(remote, kind, key):
            calls.append(remote.peer_id)
            result = Message(type=kind)
            # Every response redirects to a new unqueried peer; even a malicious
            # endless overlay must not turn a lookup into an unbounded loop.
            following = PeerInfo(peer(len(calls) + 1), [Multiaddr("/ip4/127.0.0.1/tcp/4001")])
            ranks[following.peer_id.to_bytes()] = -len(calls) - 1
            add_peer(result, "closerPeers", following)
            return result

        client._query = query
        await client._lookup(b"target", Message.GET_PROVIDERS)
        assert len(calls) == MAX_QUERIES
    trio.run(exercise)


def test_provider_lookup_distinguishes_empty_response_from_unavailable_network():
    async def exercise():
        key = b"\x12\x20" + hashlib.sha256(b"public key").digest()
        client = AminoDHTClient(FakeHost(), [])
        assert await client.find_providers(key) == []
        assert client.status()["lastLookupStatus"] == "unavailable"
        remote = PeerInfo(peer(1), [Multiaddr("/ip4/8.8.8.8/tcp/4001")])
        client._remember(remote)

        async def query(remote, kind, key):
            return Message(type=kind)

        client._query = query
        assert await client.find_providers(key) == []
        assert client.status()["lastLookupStatus"] == "empty"
        assert client.status()["lastLookupPeers"] == 1
        assert client.status()["lastLookupProviders"] == 0
    trio.run(exercise)


def test_real_noise_yamux_bootstrap_and_fire_and_forget_provider_roundtrip():
    async def exercise():
        server, advertiser, observer = secure_host(), secure_host(), secure_host()
        retained = {}
        announced = trio.Event()
        seen = []

        def respond(request, remote):
            seen.append(request.type)
            if request.type == Message.ADD_PROVIDER:
                assert request.providerPeers[0].id == remote.to_bytes()
                retained[request.key] = request.providerPeers[0]
                announced.set()
                return None  # Real Kubo behavior: no acknowledgement.
            response = Message(type=request.type)
            if request.type == Message.GET_PROVIDERS and request.key in retained:
                response.providerPeers.add().CopyFrom(retained[request.key])
            return response

        install_server(server, respond)
        async with AsyncExitStack() as stack:
            for host in (server, advertiser, observer):
                await stack.enter_async_context(host.run(listen_addrs=[Multiaddr("/ip4/127.0.0.1/tcp/0")]))
            seed = str(server.get_addrs()[0])
            publisher = AminoDHTClient(advertiser, [seed], timeout=3, allow_private=True)
            finder = AminoDHTClient(observer, [seed], timeout=3, allow_private=True)
            assert await publisher.bootstrap() == 1
            assert await finder.bootstrap() == 1
            key = b"\x12\x20" + hashlib.sha256(b"operator public key").digest()
            assert await publisher.provide(key, advertiser.get_addrs()) == 1
            with trio.fail_after(3):
                await announced.wait()
            providers = await finder.find_providers(key, count=1)
            assert [p.peer_id for p in providers] == [advertiser.get_id()]
            assert providers[0].addrs == info(advertiser).addrs
            assert finder.status()["lastLookupStatus"] == "found"
            assert publisher.status()["announcementStatus"] == "sent"
            assert publisher.status()["announcementsSent"] == 1
            assert Message.ADD_PROVIDER in seen and Message.GET_PROVIDERS in seen
            # A client must not install or advertise a Kademlia server handler.
            assert AMINO_PROTOCOL not in advertiser.get_mux().handlers
            assert AMINO_PROTOCOL not in observer.get_mux().handlers
    trio.run(exercise)


def test_real_get_providers_follows_closer_peer_from_response():
    async def exercise():
        first, second, observer = secure_host(), secure_host(), secure_host()
        provider = PeerInfo(peer(55), [Multiaddr("/ip4/127.0.0.1/tcp/4001")])
        second_queries = []

        def first_response(request, remote):
            response = Message(type=request.type)
            # FIND_NODE during bootstrap deliberately reveals no other server.
            if request.type == Message.GET_PROVIDERS:
                add_peer(response, "closerPeers", info(second))
            return response

        def second_response(request, remote):
            second_queries.append(request.type)
            response = Message(type=request.type)
            add_peer(response, "providerPeers", provider)
            return response

        install_server(first, first_response)
        install_server(second, second_response)
        async with AsyncExitStack() as stack:
            for host in (first, second, observer):
                await stack.enter_async_context(host.run(listen_addrs=[Multiaddr("/ip4/127.0.0.1/tcp/0")]))
            client = AminoDHTClient(observer, [str(first.get_addrs()[0])], timeout=3, allow_private=True)
            assert await client.bootstrap() == 1
            key = b"\x12\x20" + hashlib.sha256(b"operator public key").digest()
            providers = await client.find_providers(key, count=1)
            assert [p.peer_id for p in providers] == [provider.peer_id]
            assert second_queries == [Message.GET_PROVIDERS]
            assert client.status()["routingPeers"] == 2
    trio.run(exercise)
