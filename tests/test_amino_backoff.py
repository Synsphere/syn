"""Failed DHT peers are not hammered by independent group/key lookups."""
import hashlib
from types import SimpleNamespace

from libp2p.kad_dht.pb.kademlia_pb2 import Message
from libp2p.peer.id import ID
from libp2p.peer.peerinfo import PeerInfo
from multiaddr import Multiaddr
import trio
from trio.testing import MockClock

from syn.amino import AminoDHTClient, MAX_KNOWN_PEERS, RETRY_MAX_SECONDS


def peer(number):
    return PeerInfo(ID(b"\x12\x20" + hashlib.sha256(str(number).encode()).digest()),
                    [Multiaddr("/ip4/127.0.0.1/tcp/4001")])


def test_failure_cooldown_spans_keys_then_recovers_and_resets():
    clock = MockClock(autojump_threshold=0)

    async def exercise():
        remote = peer(1)
        client = AminoDHTClient(SimpleNamespace(get_id=lambda: peer(0).peer_id), [], allow_private=True)
        calls = []
        healthy = False

        async def request(info, packet, **kwargs):
            calls.append((trio.current_time(), packet.key))
            if not healthy:
                raise OSError("Synthetic unreachable peer")
            return Message(type=packet.type, key=packet.key)

        client._request = request
        assert await client._query(remote, Message.FIND_NODE, b"one") is None
        assert await client._query(remote, Message.GET_PROVIDERS, b"two") is None
        assert len(calls) == 1
        assert client.status()["queryFailures"] == 1
        assert client.status()["retryDeferred"] == 1
        await trio.sleep(5)
        assert await client._query(remote, Message.GET_PROVIDERS, b"two") is None
        assert len(calls) == 2
        await trio.sleep(9)
        assert await client._query(remote, Message.FIND_NODE, b"three") is None
        assert len(calls) == 2
        await trio.sleep(1)
        healthy = True
        assert await client._query(remote, Message.FIND_NODE, b"three") is not None
        assert await client._query(remote, Message.GET_PROVIDERS, b"four") is not None
        assert len(calls) == 4
        healthy = False
        assert await client._query(remote, Message.FIND_NODE, b"five") is None
        await trio.sleep(5)
        assert await client._query(remote, Message.FIND_NODE, b"six") is None
        assert len(calls) == 6  # success reset the cooldown to five seconds

    trio.run(exercise, clock=clock)


def test_cancelled_request_is_retried_without_peer_penalty():
    async def exercise():
        remote = peer(1)
        client = AminoDHTClient(SimpleNamespace(get_id=lambda: peer(0).peer_id), [])
        attempted = trio.Event()
        calls = []

        async def request(info, packet, **kwargs):
            calls.append(packet.key)
            attempted.set()
            await trio.sleep_forever()

        client._request = request
        async with trio.open_nursery() as nursery:
            nursery.start_soon(client._query, remote, Message.FIND_NODE, b"cancelled")
            await attempted.wait()
            nursery.cancel_scope.cancel()
        assert client.status()["queryFailures"] == 0

        async def succeed(info, packet, **kwargs):
            calls.append(packet.key)
            return Message(type=packet.type)

        client._request = succeed
        assert await client._query(remote, Message.FIND_NODE, b"retry") is not None
        assert calls == [b"cancelled", b"retry"]
    trio.run(exercise)


def test_cooldown_is_bounded_and_bad_peer_does_not_block_good_peer():
    async def exercise():
        good, bad = peer(1), peer(2)
        client = AminoDHTClient(SimpleNamespace(get_id=lambda: peer(0).peer_id), [])
        calls = []

        async def request(info, packet, **kwargs):
            calls.append(info.peer_id)
            if info.peer_id == good.peer_id:
                return Message(type=packet.type)
            raise OSError("unreachable")

        client._request = request
        client._remember(good)
        client._remember(bad)
        closest, _ = await client._lookup(b"lookup-one", Message.FIND_NODE)
        assert [p.peer_id for p in closest] == [good.peer_id]
        closest, _ = await client._lookup(b"lookup-two", Message.FIND_NODE)
        assert [p.peer_id for p in closest] == [good.peer_id]
        assert calls.count(bad.peer_id) == 1
        for value in range(MAX_KNOWN_PEERS + 10):
            client._failed(peer(value + 3))
        assert len(client._retries) <= MAX_KNOWN_PEERS
        for _ in range(100):
            client._failed(bad)
        assert 0 < client._retries[bad.peer_id.to_bytes()][1] - trio.current_time() <= RETRY_MAX_SECONDS
    trio.run(exercise)


def test_cooling_nearest_peers_do_not_exhaust_the_query_budget():
    async def exercise():
        client = AminoDHTClient(SimpleNamespace(get_id=lambda: peer(0).peer_id), [])
        target_key = b"healthy peers behind a failed closest set"
        target = int.from_bytes(hashlib.sha256(target_key).digest(), "big")
        ordered = sorted([peer(value) for value in range(1, 70)],
                         key=lambda value: client._distance(value, target))
        for info in ordered:
            client._remember(info)
        for info in ordered[:64]:
            client._failed(info)
        calls = []

        async def request(info, packet, **kwargs):
            calls.append(info.peer_id)
            return Message(type=packet.type, key=packet.key)

        client._request = request
        closest, _ = await client._lookup(target_key, Message.FIND_NODE)
        assert {item.peer_id for item in closest} == {item.peer_id for item in ordered[64:]}
        assert set(calls) == {item.peer_id for item in ordered[64:]}
        assert len(calls) == 5
    trio.run(exercise)
