"""Open public groups, bounded retention and adversarial lifecycle behavior."""
from contextlib import AsyncExitStack, asynccontextmanager
import hashlib
from types import SimpleNamespace

from libp2p.network.stream.exceptions import StreamError
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.pubsub.gossipsub import PROTOCOL_ID_V11
from libp2p.pubsub.pb import rpc_pb2
from libp2p.pubsub.pubsub import Pubsub
from libp2p.pubsub.validators import PUBSUB_SIGNING_PREFIX
from multiaddr import Multiaddr
import pytest
import trio
import varint

from syn.config import P2PConfig
import syn.public_pubsub as public
from syn.peer_record_transport import write_frame
from syn_client.public_pubsub import encode_message

from test_mesh_pubsub import secure_host


def configuration(**values):
    return P2PConfig(listenAddresses=("/ip4/127.0.0.1/tcp/0",), bootstrapPeers=(),
        mdnsEnabled=False, upnpEnabled=False, allowPrivateAddresses=True,
        publicPubsubDiscoveryIntervalSeconds=1, requestTimeoutSeconds=1,
        lookupTimeoutSeconds=2, **values)


@asynccontextmanager
async def running(count=1, *, dht=None, config=None, announce=False):
    async with AsyncExitStack() as stack:
        hosts = [secure_host() for _ in range(count)]
        for host in hosts:
            await stack.enter_async_context(host.run(listen_addrs=[Multiaddr("/ip4/127.0.0.1/tcp/0")]))
        services = [public.PublicPubsubService(host, dht, config or configuration(),
                    host.get_addrs if announce else lambda: []) for host in hosts]
        async with trio.open_nursery() as nursery:
            for service in services:
                await nursery.start(service.run)
            try:
                yield hosts, services
            finally:
                nursery.cancel_scope.cancel()


async def eventually(predicate, timeout=5):
    with trio.fail_after(timeout):
        while not predicate():
            await trio.sleep(.01)


def test_local_join_is_immediate_idempotent_and_open_without_a_dht_or_operator():
    async def exercise():
        async with running(config=configuration(publicPubsubMaxGroups=2)) as (_, services):
            service = services[0]
            group = await service.join("synny://cog/a-public-cog")
            assert group["url"] == "syn://cog/a-public-cog"
            assert group["joined"] and group["peer_count"] == group["mesh_peer_count"] == 0
            assert group["discovery"]["state"] == "disabled"
            assert await service.join(group["url"]) == group
            other = await service.join(group["url"], "another-network")
            assert other["group_hash"] != group["group_hash"]
            with pytest.raises(public.PublicPubsubBusy):
                await service.join("syn://event/another-public-group")
            accepted = await service.publish(group["group_hash"], {"opaque_public_data": True})
            assert accepted["accepted"] and not accepted["peer_reception_proved"]
            await eventually(lambda: service.status()["groups"][0]["received"] == 1)
            first = await service.read(group["group_hash"])
            second = await service.read(group["group_hash"])
            assert first == second and len(first["messages"]) == 1
            assert first["messages"][0]["sender_peer_id"] == accepted["sender_peer_id"]
            assert first["messages"][0]["data"] == {"opaque_public_data": True}
            assert not (await service.read(other["group_hash"]))["messages"]
            with pytest.raises(public.PublicGroupNotJoined):
                await service.publish("f" * 64, "not joined")
    trio.run(exercise)


def test_retention_cursors_independent_readers_and_leave_generation():
    async def exercise():
        async with running(config=configuration(publicPubsubQueueSize=2)) as (_, services):
            service = services[0]
            group = await service.join("syn://ember/public-notice")
            key = group["group_hash"]
            for index in range(4):
                await service.publish(key, {"index": index})
                await trio.lowlevel.checkpoint()
            await eventually(lambda: service.status()["groups"][0]["received"] == 4)
            first = await service.read(key, group["cursor"], 1)
            assert first["dropped"] == 2 and first["has_more"]
            assert first["messages"][0]["data"] == {"index": 2}
            next_page = await service.read(key, first["cursor"], 1)
            assert next_page["dropped"] == 0 and not next_page["has_more"]
            assert next_page["messages"][0]["data"] == {"index": 3}
            first["messages"][0]["data"]["index"] = "caller mutation"
            assert (await service.read(key, group["cursor"], 1))["messages"][0]["data"] == {"index": 2}
            assert not (await service.read(key, next_page["cursor"]))["messages"]
            assert not (await service.leave(key))["already_left"]
            assert (await service.leave(key))["already_left"]
            assert not service._buffer_order and service._buffered_bytes == 0
            rejoined = await service.join(group["url"])
            assert rejoined["stream_id"] != group["stream_id"]
            with pytest.raises(public.PublicCursorExpired):
                await service.read(key, next_page["cursor"])
            assert not (await service.read(key))["messages"]
    trio.run(exercise)


def test_noisy_group_does_not_accumulate_evicted_global_buffer_entries():
    async def exercise():
        async with running(config=configuration(publicPubsubQueueSize=2)) as (_, services):
            service = services[0]
            quiet = await service.join("syn://type/quiet")
            busy = await service.join("syn://type/busy")
            await service.publish(quiet["group_hash"], "retained old message")
            for index in range(100):
                await service.publish(busy["group_hash"], index)
                await trio.lowlevel.checkpoint()
            await eventually(lambda: service.status()["groups"][1]["received"] == 100)
            assert len(service._buffer_order) == 3
            assert len((await service.read(quiet["group_hash"]))["messages"]) == 1
            result = await service.read(busy["group_hash"])
            assert [value["data"] for value in result["messages"]] == [98, 99]
            assert result["dropped"] == 98
    trio.run(exercise)


def test_global_byte_budget_reports_evicted_messages(monkeypatch):
    monkeypatch.setattr(public, "MAX_BUFFER_BYTES", 1000)
    async def exercise():
        async with running(config=configuration(publicPubsubQueueSize=100)) as (_, services):
            service = services[0]
            groups = [await service.join("syn://event/first"), await service.join("syn://event/second")]
            for index in range(10):
                await service.publish(groups[index % 2]["group_hash"], {"number": index, "text": "a" * 100})
                await trio.lowlevel.checkpoint()
            await eventually(lambda: sum(value["received"] for value in service.status()["groups"]) == 10)
            assert service._buffered_bytes <= 1000
            results = [await service.read(group["group_hash"]) for group in groups]
            assert sum(result["dropped"] for result in results) >= 7
            assert len(service._buffer_order) == sum(len(result["messages"]) for result in results)
    trio.run(exercise)


def test_wire_messages_are_signed_scoped_and_application_duplicates_are_bounded():
    async def exercise():
        async with running(count=2) as (hosts, services):
            first, second = services
            group = await first.join("syn://type/public-wire")
            await second.join(group["url"])
            # Wire validation starts from a discovered provider; a generic
            # connection alone intentionally does not negotiate pubsub.
            first._groups[group["group_hash"]].providers[hosts[1].get_id()] = trio.current_time() + 60
            await hosts[0].connect(info_from_p2p_addr(hosts[1].get_addrs()[0]))
            await eventually(lambda: all(service.status()["groups"][0]["mesh_peer_count"] for service in services))
            stream = await hosts[0].new_stream(hosts[1].get_id(), [PROTOCOL_ID_V11])
            async def transmit(data, topics=None, tamper_signature=False):
                message = rpc_pb2.Message(data=data, topicIDs=topics or [group["topic"]],
                    from_id=hosts[0].get_id().to_bytes(), seqno=first.pubsub._next_seqno())
                signature = hosts[0].get_private_key().sign(
                    PUBSUB_SIGNING_PREFIX.encode() + message.SerializeToString())
                message.signature = (bytes([signature[0] ^ 1]) + signature[1:]
                                     if tamper_signature else signature)
                message.key = hosts[0].get_public_key().serialize()
                await write_frame(stream, rpc_pb2.RPC(publish=[message]).SerializeToString(), public.MAX_RPC_BYTES)
            wrong = encode_message("another-network", group["url"], "wrong network")
            await transmit(wrong)
            wrong_url = encode_message("syn", "syn://cog/another", "wrong URL")
            await transmit(wrong_url)
            valid = encode_message("syn", group["url"], {"visible": True}, message_id="ab" * 16)
            await transmit(valid, [group["topic"], "/unrelated-topic"])
            await transmit(valid, tamper_signature=True)
            await transmit(valid)
            await transmit(valid)  # different signed seqno, same application ID
            await transmit(encode_message("syn", group["url"], {"barrier": True}))
            await eventually(lambda: second.status()["groups"][0]["received"] == 2)
            values = (await second.read(group["group_hash"]))["messages"]
            assert [value["data"] for value in values] == [{"visible": True}, {"barrier": True}]
            assert all(value["sender_peer_id"] == str(hosts[0].get_id()) for value in values)
    trio.run(exercise)


def test_group_leave_cancels_pending_dht_work_without_late_announcement():
    async def exercise():
        entered, cancelled = trio.Event(), trio.Event()
        class HangingDHT:
            async def provide(self, key, addresses):
                entered.set()
                try:
                    await trio.sleep_forever()
                finally:
                    cancelled.set()
            async def find_providers(self, *_args, **_kwargs):
                pytest.fail("Left group must not continue a cancelled announcement")
        async with running(dht=HangingDHT(), announce=True) as (_, services):
            service = services[0]
            group = await service.join("syn://thing/leave-during-discovery")
            with trio.fail_after(2):
                await entered.wait()
                await service.leave(group["group_hash"])
                await cancelled.wait()
            assert service.status()["groups"] == []
            assert group["topic"] not in service.pubsub.topic_ids
            assert group["topic"] not in service.router.mesh
            await eventually(lambda: service._maintenance_count == 0)
    trio.run(exercise)


@pytest.mark.parametrize("failure", ["exception", "empty-unavailable-result"])
def test_dht_outage_does_not_remove_local_public_subscription(failure):
    async def exercise():
        attempted = trio.Event()
        class UnavailableDHT:
            async def provide(self, key, addresses):
                if failure == "exception":
                    attempted.set()
                    raise OSError("Synthetic DHT outage")
                return 0
            async def find_providers(self, key, count=20):
                attempted.set()
                return []
            def status(self):
                return {"lastLookupStatus": "unavailable"}
        async with running(dht=UnavailableDHT(), announce=True) as (_, services):
            service = services[0]
            group = await service.join("syn://event/outage")
            with trio.fail_after(2):
                await attempted.wait()
            assert service.status()["groups"][0]["discovery"]["state"] == "unavailable"
            accepted = await service.publish(group["group_hash"], "still joined")
            assert accepted["accepted"]
            await eventually(lambda: service.status()["groups"][0]["received"] == 1)
            assert (await service.read(group["group_hash"]))["messages"][0]["data"] == "still joined"
    trio.run(exercise)


def test_maintenance_is_fair_and_bounded_across_groups():
    async def exercise():
        keys, outstanding, peak = set(), 0, 0
        class DHT:
            async def find_providers(self, key, count=20):
                nonlocal outstanding, peak
                outstanding += 1
                peak = max(peak, outstanding)
                try:
                    await trio.sleep(.02)
                    keys.add(key)
                    return []
                finally:
                    outstanding -= 1
        async with running(dht=DHT()) as (_, services):
            service = services[0]
            for index in range(5):
                await service.join(f"syn://type/fair-{index}")
            await eventually(lambda: len(keys) == 5)
            assert peak == 2
            assert all(group["discovery"]["state"] == "ready" for group in service.status()["groups"])
    trio.run(exercise)


def test_cancelled_join_rolls_back_subscriptions_and_capacity(monkeypatch):
    async def exercise():
        async with running(config=configuration(publicPubsubMaxGroups=1)) as (_, services):
            service = services[0]
            original = service.pubsub.subscribe
            entered, done = trio.Event(), trio.Event()
            async def hanging(topic):
                await original(topic)
                entered.set()
                await trio.sleep_forever()
            monkeypatch.setattr(service.pubsub, "subscribe", hanging)
            async with trio.open_nursery() as nursery:
                scope = trio.CancelScope()
                async def join():
                    with scope:
                        await service.join("syn://cog/cancelled")
                    done.set()
                nursery.start_soon(join)
                await entered.wait()
                scope.cancel()
                with trio.fail_after(2):
                    await done.wait()
                nursery.cancel_scope.cancel()
            assert not service.status()["groups"]
            assert not service.pubsub.topic_ids and not service.router.mesh
            monkeypatch.setattr(service.pubsub, "subscribe", original)
            assert (await service.join("syn://cog/successor"))["joined"]
    trio.run(exercise)


def test_unknown_subscription_and_control_topics_are_bounded(monkeypatch):
    monkeypatch.setattr(public, "MAX_REMOTE_TOPICS", 8)
    async def exercise():
        async with running(count=2) as (hosts, services):
            target = services[0]
            with trio.fail_after(3):
                await hosts[1].connect(info_from_p2p_addr(hosts[0].get_addrs()[0]))
                stream = await hosts[1].new_stream(hosts[0].get_id(), [PROTOCOL_ID_V11])
                packet = rpc_pb2.RPC()
                packet.subscriptions.add(subscribe=True, topicid="/syn/private-pubsub/1/hidden")
                for index in range(20):
                    topic = "/syn/public-pubsub/1/" + hashlib.sha256(str(index).encode()).hexdigest()
                    packet.subscriptions.add(subscribe=True, topicid=topic)
                    packet.control.graft.add(topicID=topic)
                await write_frame(stream, packet.SerializeToString(), public.MAX_RPC_BYTES)
                await eventually(lambda: len(target.pubsub.peer_topics) == 8)
                assert "/syn/private-pubsub/1/hidden" not in target.pubsub.peer_topics
                assert not target.router.mesh
                group = await target.join("syn://type/own-group")
                assert group["topic"] in target.pubsub.peer_topics
                assert len(target.pubsub.peer_topics) <= 8
                async def unexpected_px(_peers):
                    pytest.fail("Public PRUNE must not start unfiltered peer-exchange dials")
                monkeypatch.setattr(target.router, "_do_px", unexpected_px)
                prune = rpc_pb2.RPC()
                command = prune.control.prune.add(topicID=group["topic"], backoff=60)
                command.peers.add(peerID=hosts[1].get_id().to_bytes())
                await write_frame(stream, prune.SerializeToString(), public.MAX_RPC_BYTES)
                await eventually(lambda: hosts[1].get_id() in target.router.back_off.get(group["topic"], {}))
                await stream.reset()
    trio.run(exercise)


def test_oversized_rpc_is_reset_before_payload_read():
    async def exercise():
        async with running(count=2) as (hosts, services):
            with trio.fail_after(3):
                await hosts[1].connect(info_from_p2p_addr(hosts[0].get_addrs()[0]))
                stream = await hosts[1].new_stream(hosts[0].get_id(), [PROTOCOL_ID_V11])
                await stream.write(varint.encode(public.MAX_RPC_BYTES + 1))
                try:
                    result = await stream.read(1)
                    assert result == b""
                except StreamError:
                    pass
    trio.run(exercise)


def test_old_inbound_stream_cannot_remove_a_new_peer_stream(monkeypatch):
    async def exercise():
        async with running(count=2) as (hosts, services):
            pubsub = services[0].pubsub
            peer = hosts[1].get_id()
            class Stream:
                muxed_conn = SimpleNamespace(peer_id=peer)
                def __init__(self):
                    self.entered, self.finish, self.reset_done = trio.Event(), trio.Event(), False
                async def reset(self):
                    self.reset_done = True
            old, fresh, outbound = Stream(), Stream(), Stream()
            async def read(stream, _maximum):
                stream.entered.set()
                await stream.finish.wait()
                raise EOFError
            monkeypatch.setattr(public, "read_frame", read)
            async with trio.open_nursery() as nursery:
                nursery.start_soon(pubsub.stream_handler, old)
                await old.entered.wait()
                pubsub.peers[peer] = outbound
                nursery.start_soon(pubsub.stream_handler, fresh)
                await fresh.entered.wait()
                old.finish.set()
                await eventually(lambda: old.reset_done)
                assert pubsub.peers.get(peer) is outbound and not outbound.reset_done
                fresh.finish.set()
                await eventually(lambda: outbound.reset_done)
                assert peer not in pubsub.peers
                nursery.cancel_scope.cancel()
    trio.run(exercise)


def test_pending_stream_negotiations_reserve_the_public_peer_capacity(monkeypatch):
    monkeypatch.setattr(public, "MAX_PUBLIC_PEERS", 2)
    async def exercise():
        async with running() as (_, services):
            service = services[0]
            group = await service.join("syn://type/pending-provider-streams")
            pubsub = service.pubsub
            opened = []
            async def hanging(self, peer):
                opened.append(peer)
                await trio.sleep_forever()
            monkeypatch.setattr(Pubsub, "_handle_new_peer", hanging)
            peers = [secure_host().get_id() for _ in range(5)]
            service._groups[group["group_hash"]].providers = {
                peer: trio.current_time() + 60 for peer in peers}
            async with trio.open_nursery() as nursery:
                for peer in peers:
                    nursery.start_soon(pubsub._handle_new_peer_safe, peer)
                await eventually(lambda: len(opened) == 2)
                await trio.lowlevel.checkpoint()
                assert len(pubsub._opening) == len(opened) == 2
                nursery.cancel_scope.cancel()
            assert not pubsub._opening
    trio.run(exercise)
