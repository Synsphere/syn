"""Real encrypted Pubsub admission, metadata isolation and revocation races."""
from contextlib import AsyncExitStack, asynccontextmanager

from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.crypto.x25519 import create_new_key_pair as noise_key_pair
from libp2p.host.exceptions import StreamFailure
from libp2p.kad_dht.pb.kademlia_pb2 import Message as DHTMessage
from libp2p.network.stream.exceptions import StreamClosed, StreamEOF, StreamReset
from libp2p.peer.id import ID
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.pubsub.gossipsub import GossipSub, PROTOCOL_ID_V11
from libp2p.pubsub.pb.rpc_pb2 import RPC
from libp2p.pubsub.validators import signature_validator
from libp2p.security.noise.transport import Transport
from libp2p.stream_muxer.yamux.yamux import Yamux
from libp2p.tools.anyio_service import background_trio_service
from multiaddr import Multiaddr
import pytest
import trio

from syn.amino import AMINO_PROTOCOL, AminoDHTClient
from syn.mesh_pubsub import AuthenticatedPubsub, MAX_PEERS
from syn.peer_record_transport import read_frame, write_frame


TOPIC = "/syn/private-pubsub/1/synthetic-private-scope"


def secure_host():
    identity = create_new_key_pair()
    return new_host(key_pair=identity, bootstrap=[], enable_mDNS=False, enable_upnp=False,
        sec_opt={"/noise": Transport(identity, noise_key_pair().private_key)},
        muxer_opt={"/yamux/1.0.0": Yamux})


def service(host, **kwargs):
    router = GossipSub([PROTOCOL_ID_V11], degree=3, degree_low=1,
        degree_high=4, heartbeat_interval=.1)
    return AuthenticatedPubsub(host, router, **kwargs), router


@asynccontextmanager
async def running(hosts, services=()):
    async with AsyncExitStack() as stack:
        for host in hosts:
            await stack.enter_async_context(host.run(
                listen_addrs=[Multiaddr("/ip4/127.0.0.1/tcp/0")]))
        for pubsub, router in services:
            await stack.enter_async_context(background_trio_service(pubsub))
            await stack.enter_async_context(background_trio_service(router))
            await pubsub.wait_until_ready()
        yield


def install_dht(host):
    requests = []

    async def respond(stream):
        try:
            request = DHTMessage()
            request.ParseFromString(await read_frame(stream, 131072))
            requests.append(request.type)
            answer = DHTMessage(type=request.type, key=request.key)
            await write_frame(stream, answer.SerializeToString(), 131072)
        finally:
            await stream.close()

    host.set_stream_handler(AMINO_PROTOCOL, respond)
    return requests


async def wait_for_subscription(pubsub, peer):
    await pubsub.wait_for_subscription(peer, TOPIC, timeout=4)
    # A subscription can precede the first GossipSub GRAFT heartbeat. A single
    # publication before that mesh exists is not a delivery guarantee.
    with trio.fail_after(4):
        while peer not in pubsub.router.mesh.get(TOPIC, ()):
            await trio.sleep(.01)


async def receive(subscription, data):
    with trio.fail_after(4):
        packet = await subscription.get()
    assert packet.data == data
    assert signature_validator(packet)
    return packet


def test_public_dht_bootstrap_never_receives_private_pubsub_metadata_or_control():
    async def exercise():
        private, bootstrap = secure_host(), secure_host()
        selected, router = service(private)
        requests = install_dht(bootstrap)
        opened, controls = [], []

        async def observe(stream):
            opened.append(stream.muxed_conn.peer_id)
            await stream.reset()

        bootstrap.set_stream_handler(PROTOCOL_ID_V11, observe)
        original_control = router.handle_rpc

        async def control(rpc, peer):
            controls.append(peer)
            await original_control(rpc, peer)

        router.handle_rpc = control
        async with running([private, bootstrap], [(selected, router)]):
            await selected.subscribe(TOPIC)
            client = AminoDHTClient(private, [str(bootstrap.get_addrs()[0])],
                timeout=2, allow_private=True)
            assert await client.bootstrap() == 1
            assert not selected.admitted_peers and not selected.peers and not opened
            # A bootstrap can support GossipSub and actively try subscriptions
            # and GRAFT. It still must not enter the private router or learn a
            # subscription from our automatic connection notifee.
            rpc = RPC()
            rpc.subscriptions.add(subscribe=True, topicid=TOPIC)
            rpc.control.graft.add(topicID=TOPIC)
            with trio.fail_after(2):
                with pytest.raises((StreamFailure, StreamReset, StreamEOF, StreamClosed, EOFError)):
                    stream = await bootstrap.new_stream(private.get_id(), [PROTOCOL_ID_V11])
                    await write_frame(stream, rpc.SerializeToString(), 8192)
                    await read_frame(stream, 8192)
            assert not controls
            assert bootstrap.get_id() not in selected.peer_topics.get(TOPIC, ())
            assert bootstrap.get_id() not in router.mesh.get(TOPIC, ())
            # Rejection resets only the Pubsub stream: the public connection
            # remains usable for real Kademlia request/response exchanges.
            assert await client.bootstrap() == 1
            assert len(requests) >= 4 and not opened

    trio.run(exercise)


def test_existing_connection_can_be_admitted_and_exchange_signed_messages_both_ways():
    async def exercise():
        left, right = secure_host(), secure_host()
        first, first_router = service(left)
        second, second_router = service(right)
        async with running([left, right], [(first, first_router), (second, second_router)]):
            first_messages = await first.subscribe(TOPIC)
            second_messages = await second.subscribe(TOPIC)
            await left.connect(info_from_p2p_addr(right.get_addrs()[0]))
            assert not first.admitted_peers and not second.admitted_peers
            await first.admit(right.get_id())
            await second.admit(left.get_id())
            await first.admit(right.get_id())  # idempotent for an existing connection
            await wait_for_subscription(first, right.get_id())
            await wait_for_subscription(second, left.get_id())
            await second.publish(TOPIC, b"signed-frame-right-to-left")
            assert (await receive(first_messages, b"signed-frame-right-to-left")).from_id == right.get_id().to_bytes()
            await receive(second_messages, b"signed-frame-right-to-left")  # local delivery
            await first.publish(TOPIC, b"signed-frame-left-to-right")
            assert (await receive(second_messages, b"signed-frame-left-to-right")).from_id == left.get_id().to_bytes()
            assert first.admitted_peers == frozenset({right.get_id()})
            assert second.admitted_peers == frozenset({left.get_id()})

    trio.run(exercise)


def test_revocation_blocks_inflight_publication_even_after_readmission():
    async def exercise():
        left, right = secure_host(), secure_host()
        first, first_router = service(left)
        second, second_router = service(right)
        validating, release, finished = trio.Event(), trio.Event(), trio.Event()

        async def paused_validation(peer, message):
            if message.data == b"old-admission-publication":
                validating.set()
                await release.wait()
                finished.set()
            return True

        first.set_topic_validator(TOPIC, paused_validation, True)
        async with running([left, right], [(first, first_router), (second, second_router)]):
            messages = await first.subscribe(TOPIC)
            await second.subscribe(TOPIC)
            await left.connect(info_from_p2p_addr(right.get_addrs()[0]))
            await first.admit(right.get_id())
            await second.admit(left.get_id())
            await wait_for_subscription(first, right.get_id())
            await wait_for_subscription(second, left.get_id())
            await second.publish(TOPIC, b"old-admission-publication")
            with trio.fail_after(3):
                await validating.wait()
            # Exercise the already received publication, not a later GossipSub
            # retransmission arriving on a newly authorized stream.
            for _ in range(second_router.mcache.history_size):
                second_router.mcache.shift()
            await first.remove(right.get_id())
            assert right.get_id() not in first.admitted_peers
            assert right.get_id() not in first.peers
            assert right.get_id() not in first.peer_topics.get(TOPIC, ())
            assert right.get_id() not in first_router.mesh.get(TOPIC, ())
            await first.admit(right.get_id())
            await second.admit(left.get_id())
            release.set()
            with trio.fail_after(3):
                await finished.wait()
            await wait_for_subscription(first, right.get_id())
            await wait_for_subscription(second, left.get_id())
            await second.publish(TOPIC, b"new-admission-publication")
            # The old, correctly signed message was already being validated.
            # Re-admitting the same ID must not revive that old generation.
            await receive(messages, b"new-admission-publication")

    trio.run(exercise)


def test_revocation_during_stream_open_prevents_first_private_hello():
    async def exercise():
        left, right = secure_host(), secure_host()
        first, router = service(left)
        opened, release, completed = trio.Event(), trio.Event(), trio.Event()
        captured = []
        original_new_stream = left.new_stream

        async def delayed_open(peer, protocols):
            if PROTOCOL_ID_V11 in protocols:
                # Model a transport negotiation completing after revocation;
                # cancellation of the caller cannot un-send an established IO.
                with trio.CancelScope(shield=True):
                    opened.set()
                    await release.wait()
                    result = await original_new_stream(peer, protocols)
                completed.set()
                return result
            return await original_new_stream(peer, protocols)

        async def observer(stream):
            try:
                captured.append(await read_frame(stream, 8192))
            except (StreamReset, StreamEOF, EOFError):
                pass
            finally:
                await stream.close()

        left.new_stream = delayed_open
        right.set_stream_handler(PROTOCOL_ID_V11, observer)
        async with running([left, right], [(first, router)]):
            await first.subscribe(TOPIC)
            await left.connect(info_from_p2p_addr(right.get_addrs()[0]))
            await first.admit(right.get_id())
            with trio.fail_after(3):
                await opened.wait()
            await first.remove(right.get_id())
            release.set()
            with trio.fail_after(3):
                await completed.wait()
            await trio.sleep(.05)
            assert not captured
            assert right.get_id() not in first.admitted_peers
            assert right.get_id() not in first.peers

    trio.run(exercise)


def test_revocation_blocks_old_stream_publication_queued_before_validation():
    async def exercise():
        left, right = secure_host(), secure_host()
        first, first_router = service(left)
        second, second_router = service(right)
        queued, release, finished = trio.Event(), trio.Event(), trio.Event()
        original_push = first.push_msg

        async def delayed_dispatch(peer, message):
            if message.data == b"queued-old-admission":
                queued.set()
                await release.wait()
            await original_push(peer, message)
            if message.data == b"queued-old-admission":
                finished.set()

        first.push_msg = delayed_dispatch
        async with running([left, right], [(first, first_router), (second, second_router)]):
            messages = await first.subscribe(TOPIC)
            await second.subscribe(TOPIC)
            await left.connect(info_from_p2p_addr(right.get_addrs()[0]))
            await first.admit(right.get_id())
            await second.admit(left.get_id())
            await wait_for_subscription(first, right.get_id())
            await wait_for_subscription(second, left.get_id())
            await second.publish(TOPIC, b"queued-old-admission")
            with trio.fail_after(3):
                await queued.wait()
            for _ in range(second_router.mcache.history_size):
                second_router.mcache.shift()
            await first.remove(right.get_id())
            await first.admit(right.get_id())
            await second.admit(left.get_id())
            release.set()
            with trio.fail_after(3):
                await finished.wait()
            await wait_for_subscription(first, right.get_id())
            await wait_for_subscription(second, left.get_id())
            await second.publish(TOPIC, b"fresh-dispatch")
            await receive(messages, b"fresh-dispatch")

    trio.run(exercise)


def test_legacy_initial_admissions_are_bounded_and_cannot_disable_signatures():
    host = secure_host()
    identities = [ID.from_pubkey(create_new_key_pair().public_key) for _ in range(MAX_PEERS + 1)]
    with pytest.raises(ValueError, match="capacity"):
        service(host, admitted_peers=identities)
    with pytest.raises(ValueError, match="strict"):
        service(secure_host(), strict_signing=False)
    with pytest.raises(ValueError, match="remote"):
        service(host, admitted_peers=[host.get_id()])


def test_admission_waits_boundedly_for_pubsub_startup():
    async def exercise():
        left, right = secure_host(), secure_host()
        first, router = service(left)
        admitted = trio.Event()

        async def early_authentication():
            await first.admit(right.get_id())
            admitted.set()

        async with running([left, right]):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(early_authentication)
                await trio.sleep(0)
                async with background_trio_service(first), background_trio_service(router):
                    with trio.fail_after(2):
                        await admitted.wait()
                    assert right.get_id() in first.admitted_peers

    trio.run(exercise)


def test_cancelled_startup_admission_leaves_no_permission():
    async def exercise():
        first, _ = service(secure_host())
        peer = secure_host().get_id()
        with trio.move_on_after(.02) as cancelled:
            await first.admit(peer)
        assert cancelled.cancelled_caught
        assert not first.admitted_peers

    trio.run(exercise)
