"""Separate provider/inbound group discovery from ordinary host connections."""
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.pubsub.gossipsub import PROTOCOL_ID_V11
from libp2p.pubsub.pb import rpc_pb2
from libp2p.pubsub.pubsub import Pubsub
import pytest
import trio
from trio.testing import MockClock, wait_all_tasks_blocked

import syn.public_pubsub as public
from syn.peer_record_transport import write_frame

from test_public_pubsub_engine import eventually, running
from test_mesh_pubsub import secure_host


def test_ordinary_host_connections_never_negotiate_public_pubsub(monkeypatch):
    async def exercise():
        async with running(count=2) as (hosts, services):
            # Even identical local group subscriptions do not turn an ordinary
            # DHT/host connection into a discovered group provider.
            for service in services:
                await service.join("syn://type/ordinary-dht-connection")
            attempts = []
            for host in hosts:
                original_open = host.new_stream

                async def opening(peer, protocols, original=original_open):
                    if PROTOCOL_ID_V11 in protocols:
                        attempts.append(peer)
                    return await original(peer, protocols)

                monkeypatch.setattr(host, "new_stream", opening)
            await hosts[0].connect(info_from_p2p_addr(hosts[1].get_addrs()[0]))
            # Verify the absence of a GossipSub dial after connection tasks
            # have reached quiescence; don't rely on an upstream private callback.
            await wait_all_tasks_blocked()
            assert attempts == []
            assert all(not service.pubsub.peers for service in services)
            assert hosts[0].get_network().get_connections(hosts[1].get_id())
    trio.run(exercise)


def test_inbound_public_participant_can_join_without_a_dht_result():
    async def exercise():
        async with running(count=2) as (hosts, services):
            first, second = services
            group = await first.join("syn://event/open-inbound-membership")
            await second.join(group["url"])
            await hosts[0].connect(info_from_p2p_addr(hosts[1].get_addrs()[0]))
            # An inbound peer is free to negotiate GossipSub and announce a
            # shared public topic. No identity, Operator or local DHT hit is needed.
            stream = await hosts[0].new_stream(hosts[1].get_id(), [PROTOCOL_ID_V11])
            hello = rpc_pb2.RPC()
            hello.subscriptions.add(subscribe=True, topicid=group["topic"])
            await write_frame(stream, hello.SerializeToString(), public.MAX_RPC_BYTES)
            await eventually(lambda: all(service.status()["groups"][0]["mesh_peer_count"]
                                         for service in services))
            assert all(not service._groups[group["group_hash"]].providers for service in services)
            await first.publish(group["group_hash"], {"public": "inbound"})
            await eventually(lambda: second.status()["groups"][0]["received"] == 1)
            assert (await second.read(group["group_hash"]))["messages"][0]["data"] == {"public": "inbound"}
    trio.run(exercise)


def test_leave_during_stream_open_resets_before_subscription_hello(monkeypatch):
    async def exercise():
        async with running() as (hosts, services):
            service = services[0]
            joined = await service.join("syn://cog/cancelled-provider")
            group = service._groups[joined["group_hash"]]
            peer = secure_host().get_id()
            group.providers[peer] = trio.current_time() + 60
            entered, finish, done = trio.Event(), trio.Event(), trio.Event()

            class Stream:
                reset_done = False
                written = False
                async def reset(self):
                    self.reset_done = True
                async def write(self, value):
                    self.written = True
                    raise AssertionError("A left group sent a subscription hello")
                def get_protocol(self):
                    return PROTOCOL_ID_V11

            stream = Stream()
            async def opening(_peer, _protocols):
                # A lower layer may finish negotiation despite cancellation.
                with trio.CancelScope(shield=True):
                    entered.set()
                    await finish.wait()
                return stream
            monkeypatch.setattr(hosts[0], "new_stream", opening)
            async def negotiate():
                await service.pubsub._handle_new_peer_safe(peer)
                done.set()
            async with trio.open_nursery() as nursery:
                nursery.start_soon(negotiate)
                await entered.wait()
                await service.leave(group.key)
                finish.set()
                with trio.fail_after(2):
                    await done.wait()
                nursery.cancel_scope.cancel()
            assert stream.reset_done and not stream.written
            assert peer not in service.pubsub.peers
    trio.run(exercise)


@pytest.mark.parametrize("shielded_write", [False, True])
def test_leave_during_subscription_hello_cannot_register_a_late_peer(monkeypatch, shielded_write):
    async def exercise():
        async with running() as (hosts, services):
            service = services[0]
            joined = await service.join("syn://cog/leave-during-provider-hello")
            group = service._groups[joined["group_hash"]]
            peer = secure_host().get_id()
            group.providers[peer] = trio.current_time() + 60
            entered, finish, done = trio.Event(), trio.Event(), trio.Event()

            class Stream:
                reset_done = False
                async def reset(self):
                    self.reset_done = True
                async def write(self, value):
                    with trio.CancelScope(shield=shielded_write):
                        entered.set()
                        await finish.wait()
                def get_protocol(self):
                    return PROTOCOL_ID_V11

            stream = Stream()
            async def opening(_peer, _protocols):
                return stream
            monkeypatch.setattr(hosts[0], "new_stream", opening)
            async def negotiate():
                await service.pubsub._handle_new_peer_safe(peer)
                done.set()
            async with trio.open_nursery() as nursery:
                nursery.start_soon(negotiate)
                await entered.wait()
                await service.leave(group.key)
                finish.set()
                with trio.fail_after(2):
                    await done.wait()
                nursery.cancel_scope.cancel()
            assert stream.reset_done
            assert peer not in service.pubsub.peers
            assert not service.pubsub._opening
            assert not service.pubsub._opening_scopes
            assert not service.pubsub._opening_streams
    trio.run(exercise)


def test_failed_provider_is_backed_off_and_expired_evidence_is_inert(monkeypatch):
    clock = MockClock()
    async def exercise():
        async with running() as (_, services):
            service = services[0]
            joined = await service.join("syn://ember/provider-retry")
            group = service._groups[joined["group_hash"]]
            peer = secure_host().get_id()
            group.providers[peer] = trio.current_time() + 60
            attempted = []
            async def rejected(_self, candidate):
                attempted.append(candidate)
            monkeypatch.setattr(Pubsub, "_handle_new_peer", rejected)
            for _ in range(10):
                await service.pubsub._handle_new_peer_safe(peer)
            assert attempted == [peer]
            clock.jump(15)
            await service.pubsub._handle_new_peer_safe(peer)
            assert attempted == [peer, peer]
            clock.jump(60)
            await service.pubsub._handle_new_peer_safe(peer)
            assert attempted == [peer, peer]
    trio.run(exercise, clock=clock)


def test_inbound_subscription_churn_does_not_reset_failed_peer_backoff(monkeypatch):
    clock = MockClock()
    async def exercise():
        async with running() as (_, services):
            service = services[0]
            joined = await service.join("syn://ember/inbound-retry-churn")
            group = service._groups[joined["group_hash"]]
            peer = secure_host().get_id()
            group.providers[peer] = trio.current_time() + 60
            attempted = []
            async def rejected(_self, candidate):
                attempted.append(candidate)
            monkeypatch.setattr(Pubsub, "_handle_new_peer", rejected)
            await service.pubsub._handle_new_peer_safe(peer)
            assert attempted == [peer]

            # A first real inbound subscription can recover immediately from
            # provider-only failure, without waiting for its original cooldown.
            service.pubsub._inbound[peer] = 1
            service.pubsub._incoming_epoch[peer] = object()
            subscribe = rpc_pb2.RPC.SubOpts(subscribe=True, topicid=group.topic)
            unsubscribe = rpc_pb2.RPC.SubOpts(subscribe=False, topicid=group.topic)
            service.pubsub.handle_subscription(peer, subscribe)
            await wait_all_tasks_blocked()
            assert attempted == [peer, peer]

            for _ in range(10):
                service.pubsub.handle_subscription(peer, unsubscribe)
                service.pubsub.handle_subscription(peer, subscribe)
                await wait_all_tasks_blocked()
            # Replacing the inbound stream is not new evidence that the peer
            # can accept our reciprocal stream either.
            service.pubsub._incoming_epoch[peer] = object()
            service.pubsub.handle_subscription(peer, unsubscribe)
            service.pubsub.handle_subscription(peer, subscribe)
            await wait_all_tasks_blocked()
            assert attempted == [peer, peer]

            clock.jump(30)
            service.pubsub.handle_subscription(peer, unsubscribe)
            service.pubsub.handle_subscription(peer, subscribe)
            await wait_all_tasks_blocked()
            assert attempted == [peer, peer, peer]
    trio.run(exercise, clock=clock)


def test_unrelated_inbound_topic_does_not_trigger_a_reciprocal_open(monkeypatch):
    async def exercise():
        async with running(count=2) as (hosts, services):
            service = services[0]
            await service.join("syn://type/our-group")
            attempts = []
            original = hosts[0].new_stream
            async def opening(peer, protocols):
                if PROTOCOL_ID_V11 in protocols:
                    attempts.append(peer)
                return await original(peer, protocols)
            monkeypatch.setattr(hosts[0], "new_stream", opening)
            await hosts[1].connect(info_from_p2p_addr(hosts[0].get_addrs()[0]))
            stream = await hosts[1].new_stream(hosts[0].get_id(), [PROTOCOL_ID_V11])
            unrelated = "/syn/public-pubsub/1/" + "a" * 64
            packet = rpc_pb2.RPC()
            packet.subscriptions.add(subscribe=True, topicid=unrelated)
            await write_frame(stream, packet.SerializeToString(), public.MAX_RPC_BYTES)
            await eventually(lambda: hosts[1].get_id() in service.pubsub.peer_topics.get(unrelated, ()))
            await trio.lowlevel.checkpoint()
            assert attempts == [] and not service.pubsub.peers
    trio.run(exercise)


def test_failed_provider_connection_is_not_redialed_each_lookup(monkeypatch):
    async def exercise():
        async with running(count=2) as (hosts, services):
            service = services[0]
            joined = await service.join("syn://type/failed-provider-connection")
            group = service._groups[joined["group_hash"]]
            candidate = info_from_p2p_addr(hosts[1].get_addrs()[0])
            group.providers[candidate.peer_id] = trio.current_time() + 60
            attempted = []
            async def connect(value):
                attempted.append(value.peer_id)
                raise OSError("Synthetic unavailable provider")
            monkeypatch.setattr(hosts[0], "connect", connect)
            for _ in range(10):
                await service._connect(group, candidate)
            assert attempted == [candidate.peer_id]
    trio.run(exercise)


def test_provider_result_after_group_leave_cannot_open_a_stream(monkeypatch):
    async def exercise():
        entered, finish = trio.Event(), trio.Event()
        peer = secure_host().get_id()
        class LateDHT:
            async def find_providers(self, *_args, **_kwargs):
                # Model an already-running lookup which cannot be interrupted
                # immediately and returns after its subscription was removed.
                with trio.CancelScope(shield=True):
                    entered.set()
                    await finish.wait()
                return [type("Candidate", (), {"peer_id": peer})()]
        async with running(dht=LateDHT()) as (hosts, services):
            service = services[0]
            async def unexpected(*_args, **_kwargs):
                raise AssertionError("A stale provider result opened a connection")
            monkeypatch.setattr(hosts[0], "connect", unexpected)
            joined = await service.join("syn://event/late-provider-result")
            group = service._groups[joined["group_hash"]]
            await entered.wait()
            await service.leave(group.key)
            finish.set()
            await eventually(lambda: service._maintenance_count == 0)
            assert not group.providers and not service.pubsub._opening
    trio.run(exercise)
