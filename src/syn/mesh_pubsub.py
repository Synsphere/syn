"""Keep public discovery connections outside an authenticated private Pubsub.

The group-authentication layer calls ``admit`` only after verifying a native
Agent proof bound to the actual remote transport. DHT results alone never admit
peers. These gates cover stream hellos, subscriptions/control, and publications;
an application topic validator alone cannot protect subscription metadata.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from libp2p.network.stream.exceptions import StreamReset
from libp2p.peer.id import ID
from libp2p.exceptions import ValidationError
from libp2p.pubsub.pubsub import Pubsub
import trio

from syn_client.pubsub import MAX_PEERS


MAX_STREAMS_PER_PEER = 4
NEGOTIATION_SECONDS = 5


async def _reset(stream):
    with trio.move_on_after(1, shield=True):
        try:
            await stream.reset()
        except Exception:
            pass


class _AdmittedStream:
    """Reject data released by an old admission after revoke/readmit races."""

    def __init__(self, pubsub, stream, peer, epoch):
        self._pubsub, self._stream = pubsub, stream
        self.peer, self.epoch = peer, epoch

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def _check(self):
        if not self._pubsub._current(self.peer, self.epoch):
            raise StreamReset("Private Pubsub admission is no longer valid")

    async def read(self, n=None):
        self._check()
        value = await self._stream.read(n)
        self._check()
        return value

    async def write(self, data):
        self._check()
        await self._stream.write(data)

    async def close(self):
        try:
            await self._stream.close()
        finally:
            self._pubsub._discard(self)

    async def reset(self):
        try:
            await self._stream.reset()
        finally:
            self._pubsub._discard(self)


class _PubsubHost:
    """The Pubsub instance's host view; discovery keeps the original host."""

    def __init__(self, pubsub, host):
        self._pubsub, self._host = pubsub, host

    def __getattr__(self, name):
        return getattr(self._host, name)

    async def connect(self, info):
        if self._pubsub.is_peer_blacklisted(info.peer_id):
            raise StreamReset("Peer has no private Pubsub admission")
        return await self._host.connect(info)

    async def new_stream(self, peer, protocols):
        epoch = self._pubsub._admissions.get(peer)
        if epoch is None or not self._pubsub._current(peer, epoch):
            raise StreamReset("Peer has no private Pubsub admission")
        stream = await self._host.new_stream(peer, protocols)
        # Opening a stream yields. Revocation must precede the first hello even
        # when that stream finishes opening after an admission was removed.
        if not self._pubsub._current(peer, epoch):
            await _reset(stream)
            raise StreamReset("Private Pubsub admission changed during negotiation")
        return await self._pubsub._guard(stream, peer, epoch)


class AuthenticatedPubsub(Pubsub):
    """Pubsub with explicit, bounded admission managed on its Trio runtime.

    Initially no remote peer can exchange Pubsub metadata. ``await admit(id)``
    records a previously verified group binding and schedules negotiation,
    returning immediately so the authentication response can be sent first.
    ``await remove(id)`` revokes all of that peer's Pubsub streams while leaving
    its underlying libp2p/DHT connection intact. Caller-owned group proofs and
    their lifetime are deliberately outside this transport adapter.
    """

    def __init__(self, host, router, *, admitted_peers=(), **kwargs):
        if kwargs.get("strict_signing", True) is not True:
            raise ValueError("Private Pubsub requires strict message signatures")
        self._admissions = {}
        self._streams = {}
        self._operations = {}
        self._opening = {}
        self._scheduled = {}
        self._owner = None
        self._context = ContextVar("syn_pubsub_admission", default=None)
        super().__init__(_PubsubHost(self, host), router, **kwargs)
        # Only legacy, explicitly pinned rosters use initial admissions. The
        # provider-discovery profile leaves this argument empty.
        for peer in admitted_peers:
            self._validate_peer(peer)
            if peer not in self._admissions and len(self._admissions) >= MAX_PEERS:
                raise ValueError("Private Pubsub admission capacity is exhausted")
            self._admissions[peer] = object()

    @property
    def admitted_peers(self):
        return frozenset(self._admissions)

    def _require_loop(self):
        token = trio.lowlevel.current_trio_token()
        if self._owner is None:
            self._owner = token
        elif self._owner is not token:
            raise RuntimeError("Private Pubsub admission belongs to another Trio runtime")

    def _current(self, peer, epoch):
        return (epoch is not None and self._admissions.get(peer) is epoch
                and not super().is_peer_blacklisted(peer))

    def is_peer_blacklisted(self, peer):
        return ((peer != self.my_id and peer not in self._admissions)
                or super().is_peer_blacklisted(peer))

    def _validate_peer(self, peer_id):
        if not isinstance(peer_id, ID) or peer_id == self.my_id:
            raise ValueError("Admission requires a remote authenticated PeerID")

    async def admit(self, peer_id):
        self._require_loop()
        self._validate_peer(peer_id)
        added = peer_id not in self._admissions
        if added:
            if len(self._admissions) >= MAX_PEERS:
                raise ValueError("Private Pubsub admission capacity is exhausted")
            self._admissions[peer_id] = object()
        epoch = self._admissions[peer_id]
        try:
            with trio.fail_after(NEGOTIATION_SECONDS):
                await self.wait_until_ready()
        except BaseException:
            if added and self._current(peer_id, epoch):
                await self.remove(peer_id)
            raise
        if not self._current(peer_id, epoch):
            return
        if not self.manager.is_running:
            await self.remove(peer_id)
            raise RuntimeError("Private Pubsub service is not running")
        # An existing DHT connection already triggered the host notifee while
        # unadmitted. Explicitly start negotiation without reconnecting it.
        if self._scheduled.get(peer_id) is not epoch:
            self._scheduled[peer_id] = epoch
            self.manager.run_task(self._negotiate, peer_id, epoch)

    async def _negotiate(self, peer_id, epoch):
        try:
            # The opposite side may still be reading the group-auth response
            # when our first Pubsub stream arrives. Retry boundedly until both
            # peers' subscription streams are established.
            with trio.move_on_after(NEGOTIATION_SECONDS):
                while self._current(peer_id, epoch):
                    await self._handle_new_peer_safe(peer_id)
                    if peer_id in self.peers:
                        # An outgoing stream may have been reset while its
                        # reader notification is still queued. Reannounce the
                        # current subscriptions to detect that stale stream;
                        # write_msg performs the usual dead-peer cleanup.
                        await self.write_msg(self.peers[peer_id], self.get_hello_packet())
                    if peer_id in self.peers and any(
                        peer_id in self.peer_topics.get(topic, ()) for topic in self.topic_ids
                    ):
                        return
                    await trio.sleep(.1)
        finally:
            if self._scheduled.get(peer_id) is epoch:
                self._scheduled.pop(peer_id, None)

    async def remove(self, peer_id):
        self._require_loop()
        self._admissions.pop(peer_id, None)
        for scope in self._operations.pop(peer_id, ()):
            scope.cancel()
        streams = tuple(self._streams.pop(peer_id, ()))
        self._opening.pop(peer_id, None)
        self._scheduled.pop(peer_id, None)
        super()._handle_dead_peer(peer_id)
        # Upstream's cleanup returns early if no outbound stream was registered.
        # An inbound-only peer can still have subscriptions/router state.
        for peers in self.peer_topics.values():
            peers.discard(peer_id)
        self.router.remove_peer(peer_id)
        with trio.move_on_after(1, shield=True):
            async with trio.open_nursery() as nursery:
                for stream in streams:
                    nursery.start_soon(_reset, stream)

    @contextmanager
    def _operation(self, peer, epoch):
        context = self._context.set((peer, epoch))
        try:
            with trio.CancelScope() as scope:
                self._operations.setdefault(peer, set()).add(scope)
                try:
                    yield
                finally:
                    self._operations.get(peer, set()).discard(scope)
        finally:
            self._context.reset(context)

    async def _guard(self, stream, peer, epoch):
        streams = self._streams.setdefault(peer, set())
        if len(streams) >= MAX_STREAMS_PER_PEER:
            await _reset(stream)
            raise StreamReset("Private Pubsub stream capacity is exhausted")
        guarded = _AdmittedStream(self, stream, peer, epoch)
        streams.add(guarded)
        return guarded

    def _discard(self, stream):
        self._streams.get(stream.peer, set()).discard(stream)

    async def _handle_new_peer(self, peer_id):
        epoch = self._admissions.get(peer_id)
        if not self._current(peer_id, epoch) or self._opening.get(peer_id) is epoch:
            return
        self._opening[peer_id] = epoch
        try:
            with self._operation(peer_id, epoch), trio.move_on_after(NEGOTIATION_SECONDS):
                await super()._handle_new_peer(peer_id)
        finally:
            if self._opening.get(peer_id) is epoch:
                self._opening.pop(peer_id, None)

    async def stream_handler(self, stream):
        peer = stream.muxed_conn.peer_id
        epoch = self._admissions.get(peer)
        if not self._current(peer, epoch):
            await _reset(stream)
            return
        with trio.move_on_after(NEGOTIATION_SECONDS) as startup:
            await self.wait_until_ready()
        if startup.cancelled_caught or not self._current(peer, epoch):
            await _reset(stream)
            return
        try:
            guarded = await self._guard(stream, peer, epoch)
        except StreamReset:
            return
        try:
            with self._operation(peer, epoch):
                await super().stream_handler(guarded)
        finally:
            self._discard(guarded)

    async def handle_sending_messages(self, peer_id, stream, queue):
        try:
            with self._operation(peer_id, stream.epoch):
                await super().handle_sending_messages(peer_id, stream, queue)
        finally:
            self._discard(stream)

    def _handle_dead_peer(self, peer_id):
        previous = self._context.get()
        if previous is not None and previous[0] == peer_id:
            if self._admissions.get(peer_id) is not previous[1]:
                return  # An old stream cannot tear down a fresh admission.
        super()._handle_dead_peer(peer_id)

    def handle_subscription(self, origin_id, sub_message):
        if not self.is_peer_blacklisted(origin_id):
            super().handle_subscription(origin_id, sub_message)

    async def validate_msg(self, forwarder, message):
        source = self._context.get()
        if source is not None and source[0] == forwarder:
            # Upstream dispatches publications as service tasks. A task still
            # carries the receiving stream's admission, even if it first runs
            # after that peer has been removed and admitted again.
            if not self._current(forwarder, source[1]):
                raise ValidationError("Publication arrived under an old admission")
        origin = ID(message.from_id)
        peers = {peer for peer in (forwarder, origin) if peer != self.my_id}
        epochs = {peer: self._admissions.get(peer) for peer in peers}
        if any(not self._current(peer, epoch) for peer, epoch in epochs.items()):
            raise ValidationError("Publication has no private Pubsub admission")
        await super().validate_msg(forwarder, message)
        if any(not self._current(peer, epoch) for peer, epoch in epochs.items()):
            raise ValidationError("Admission changed during publication validation")
