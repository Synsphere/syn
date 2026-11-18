"""Open, URL-addressed GossipSub groups on the managed Syn libp2p host.

The network name is a namespace, not an admission certificate. Transport
signatures identify message authors; public messages confer no object ownership,
private membership, execution permission, or durable delivery guarantee.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from copy import deepcopy
from dataclasses import dataclass, field
import re
import secrets
import time

from google.protobuf.message import DecodeError
from libp2p.exceptions import ValidationError
from libp2p.network.stream.exceptions import StreamError, StreamReset
from libp2p.peer.id import ID
from libp2p.pubsub.gossipsub import GossipSub, PROTOCOL_ID_V11
from libp2p.pubsub.pb import rpc_pb2
from libp2p.pubsub.pubsub import Pubsub
from libp2p.pubsub.utils import maybe_consume_signed_record
from libp2p.tools.anyio_service import background_trio_service
import trio

from syn_client.public_pubsub import (
    MAX_MESSAGE_BYTES, PublicPubsubError, decode_message, encode_message,
    group_descriptor,
)

from .amino import seed_dial_addresses
from .peer_record_transport import read_frame


MAX_PUBLIC_PEERS = 128
MAX_REMOTE_TOPICS = 512
MAX_RPC_BYTES = 1024 * 1024
MAX_RPC_ITEMS = 128
MAX_BUFFER_BYTES = 16 * 1024 * 1024
_GROUP_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PUBLIC_TOPIC = re.compile(r"/syn/public-pubsub/1/[0-9a-f]{64}\Z")
_CURSOR = re.compile(r"([0-9a-f]{32}):(0|[1-9][0-9]{0,18})\Z")


class PublicGroupNotJoined(LookupError):
    pass


class PublicPubsubBusy(RuntimeError):
    pass


class PublicPubsubUnavailable(RuntimeError):
    pass


class PublicCursorExpired(ValueError):
    pass


@dataclass(eq=False)
class _Group:
    descriptor: dict
    stream_id: str = field(default_factory=lambda: secrets.token_hex(16))
    sequence: int = 0
    published: int = 0
    received: int = 0
    dropped: int = 0
    history: deque = field(default_factory=deque)
    seen: OrderedDict = field(default_factory=OrderedDict)
    reader_scope: trio.CancelScope | None = None
    discovery_scope: trio.CancelScope | None = None
    next_lookup: float = 0
    next_announcement: float = 0
    announce_retry: float = 1
    previous_addresses: tuple = ()
    providers: dict = field(default_factory=dict)
    pending: bool = False
    discovery: dict = field(default_factory=lambda: {
        "state": "waiting", "publication": "waiting-addresses",
        "candidates": 0, "announcements_sent": 0, "last_error": None,
    })

    @property
    def key(self):
        return self.descriptor["group_hash"]

    @property
    def topic(self):
        return self.descriptor["topic"]

    def cursor(self, offset=None):
        return f"{self.stream_id}:{self.sequence if offset is None else offset}"


async def _reset(stream):
    with trio.move_on_after(1, shield=True):
        try:
            await stream.reset()
        except Exception:
            pass


class _PublicRouter(GossipSub):
    """Do not retain attacker-chosen control topics outside local subscriptions."""

    async def handle_rpc(self, rpc, sender_peer_id):
        selected = rpc_pb2.RPC()
        if rpc.HasField("senderRecord"):
            selected.senderRecord = rpc.senderRecord
        topics = self.pubsub.topic_ids
        for name, field in (("ihave", "topicID"), ("graft", "topicID"), ("prune", "topicID")):
            target = getattr(selected.control, name)
            for message in getattr(rpc.control, name)[:MAX_RPC_ITEMS]:
                if getattr(message, field) in topics:
                    copied = target.add()
                    copied.CopyFrom(message)
                    if name == "ihave":
                        del copied.messageIDs[MAX_RPC_ITEMS:]
                    elif name == "prune":
                        # Upstream consumes incoming PX even with do_px=False.
                        # Keep discovery dials on the filtered Amino path.
                        del copied.peers[:]
                        copied.backoff = min(copied.backoff, 3600)
        for message in rpc.control.iwant[:MAX_RPC_ITEMS]:
            selected.control.iwant.add().messageIDs.extend(message.messageIDs[:MAX_RPC_ITEMS])
        # This service negotiates v1.1 only, so extension controls are ignored.
        await super().handle_rpc(selected, sender_peer_id)


class _OpeningStream:
    """Keep the selection valid across the initial subscription write as well."""

    def __init__(self, pubsub, stream, peer, groups):
        self._pubsub, self._stream = pubsub, stream
        self._peer, self._groups = peer, groups

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def _check(self):
        if (self._pubsub._opening.get(self._peer) is self._groups
                and not self._pubsub.selected_groups(self._peer, self._groups)):
            raise StreamReset("Public group selection changed during subscription hello")

    async def write(self, data):
        self._check()
        await self._stream.write(data)
        self._check()


class _PublicHost:
    """Guard only pubsub streams; the shared host remains available to the DHT."""

    def __init__(self, pubsub, host):
        self._pubsub, self._host = pubsub, host

    def __getattr__(self, name):
        return getattr(self._host, name)

    async def new_stream(self, peer, protocols):
        selected = self._pubsub._opening.get(peer, ())
        if not self._pubsub.selected_groups(peer, selected):
            raise StreamReset("Peer is not a provider or inbound participant of a joined public group")
        stream = await self._host.new_stream(peer, protocols)
        # Negotiation yields. A leave, replacement or expired provider record
        # must take effect before upstream sends its subscription hello.
        if not self._pubsub.selected_groups(peer, selected):
            await _reset(stream)
            raise StreamReset("Public group selection changed during negotiation")
        stream = _OpeningStream(self._pubsub, stream, peer, selected)
        self._pubsub._opening_streams[peer] = stream
        return stream


class _PublicPubsub(Pubsub):
    """Bound public RPCs and metadata without an identity admission list."""

    def __init__(self, host, router, owner):
        self.owner = owner
        self._opening = {}
        self._opening_scopes = {}
        self._opening_streams = {}
        self._retry = OrderedDict()
        self._inbound = {}
        self._inbound_count = 0
        self._incoming_epoch = {}
        self._message_slots = trio.Semaphore(32)
        # 0.8.1 exposes independent inbound frame and subscription quotas.
        # Keep the upstream parser limits no looser than Syn's existing bounds.
        super().__init__(_PublicHost(self, host), router, strict_signing=True,
                         validation_cache_size=1024, max_concurrent_validator_count=16,
                         max_inbound_rpc_size=MAX_RPC_BYTES,
                         max_subscriptions_per_rpc=MAX_RPC_ITEMS,
                         max_subscriptions_per_peer=MAX_RPC_ITEMS)

    def selected_groups(self, peer, groups=None):
        """Public discovery evidence, never a private identity allowlist.

        The shared host notifies Pubsub about DHT and bootstrap connections as
        well. Those notifications alone supply no reason to negotiate pubsub.
        Any inbound peer can instead announce a matching topic on its already
        negotiated GossipSub stream, without local DHT discovery or credentials.
        """
        now = trio.current_time()
        groups = self.owner._groups.values() if groups is None else groups
        return tuple(group for group in groups if self.owner._current(group) and (
            group.providers.get(peer, 0) > now or
            (self._inbound.get(peer, 0) and peer in self.peer_topics.get(group.topic, ()))))

    def can_attempt(self, peer):
        retry = self._retry.get(peer)
        return bool(self.selected_groups(peer)) and (retry is None or retry[0] <= trio.current_time())

    def failed_attempt(self, peer):
        _, previous, inbound_used = self._retry.get(peer, (0, 7.5, False))
        delay = min(300, previous * 2)
        self._retry[peer] = (trio.current_time() + delay, delay, inbound_used)
        self._retry.move_to_end(peer)
        while len(self._retry) > MAX_PUBLIC_PEERS * 2:
            self._retry.popitem(last=False)

    async def _handle_new_peer(self, peer):
        if (peer in self.peers or peer in self._opening or not self.can_attempt(peer)
                or len(self.peers) + len(self._opening) >= MAX_PUBLIC_PEERS):
            return
        selected = self._opening[peer] = self.selected_groups(peer)
        cancelled = False
        try:
            with trio.move_on_after(5) as scope:
                self._opening_scopes[peer] = scope
                await super()._handle_new_peer(peer)
        except trio.Cancelled:
            cancelled = True
            raise
        finally:
            stream = self._opening_streams.pop(peer, None)
            self._opening_scopes.pop(peer, None)
            self._opening.pop(peer, None)
            if peer in self.peers and not self.selected_groups(peer, selected):
                self._handle_dead_peer(peer)
            if peer not in self.peers and stream is not None:
                await _reset(stream)
            if peer in self.peers:
                self._retry.pop(peer, None)
            elif not cancelled and self.selected_groups(peer):
                self.failed_attempt(peer)

    def handle_subscription(self, peer, message):
        topic = message.topicid
        if _PUBLIC_TOPIC.fullmatch(topic) is None:
            return
        if message.subscribe:
            if topic not in self.peer_topics and len(self.peer_topics) >= MAX_REMOTE_TOPICS:
                return
            peers = self.peer_topics.get(topic, ())
            if peer not in peers and len(peers) >= MAX_PUBLIC_PEERS:
                return
        known = peer in self.peer_topics.get(topic, ())
        super().handle_subscription(peer, message)
        if (message.subscribe and not known and self._inbound.get(peer, 0)
                and topic in self.owner._topics and self.manager.is_running):
            # Actual inbound participation gets one immediate recovery attempt
            # after a provider-only failure. Repeated subscribe/unsubscribe or
            # stream replacement must not reset an unsuccessful peer's backoff.
            retry = self._retry.get(peer, (0, 7.5, False))
            if not retry[2]:
                self._retry[peer] = (0, retry[1], True)
                self._retry.move_to_end(peer)
                while len(self._retry) > MAX_PUBLIC_PEERS * 2:
                    self._retry.popitem(last=False)
            self.manager.run_task(self._handle_new_peer_safe, peer)
        if not self.peer_topics.get(topic) and topic not in self.topic_ids:
            self.peer_topics.pop(topic, None)

    def _handle_dead_peer(self, peer):
        super()._handle_dead_peer(peer)
        for topic in list(self.peer_topics):
            self.peer_topics[topic].discard(peer)
            if not self.peer_topics[topic] and topic not in self.topic_ids:
                self.peer_topics.pop(topic, None)

    def _mark_msg_seen(self, message):
        super()._mark_msg_seen(message)
        # Upstream's seen cache has a TTL but no count bound.
        with self.seen_messages.lock:
            while len(self.seen_messages.cache) > 4096:
                self.seen_messages.cache.pop(next(iter(self.seen_messages.cache)))

    async def validate_msg(self, forwarder, message):
        # Always check the current group, even if upstream cached validation.
        group = self.owner._topics.get(message.topicIDs[0]) if len(message.topicIDs) == 1 else None
        if group is None or not self.owner._current(group) or len(message.data) > MAX_MESSAGE_BYTES:
            raise ValidationError("Publication is outside a joined public group")
        try:
            decode_message(message.data, network_id=group.descriptor["network_id"],
                           url=group.descriptor["url"])
        except (PublicPubsubError, ValueError, TypeError):
            raise ValidationError("Invalid public Syn publication") from None
        await super().validate_msg(forwarder, message)
        if not self.owner._current(group):
            raise ValidationError("Public group was left during validation")

    async def _bounded_push(self, peer, message):
        try:
            await self.push_msg(peer, message)
        finally:
            self._message_slots.release()

    async def stream_handler(self, stream):
        peer = stream.muxed_conn.peer_id
        if self._inbound_count >= MAX_PUBLIC_PEERS or self._inbound.get(peer, 0) >= 2:
            await _reset(stream)
            return
        self._inbound_count += 1
        self._inbound[peer] = self._inbound.get(peer, 0) + 1
        epoch = self._incoming_epoch[peer] = object()
        try:
            with trio.fail_after(5):
                await self.wait_until_ready()
            while self.manager.is_running:
                packet = rpc_pb2.RPC()
                packet.ParseFromString(await read_frame(stream, MAX_RPC_BYTES))
                if not self.manager.is_running:
                    return
                if not maybe_consume_signed_record(packet, self.host, peer):
                    continue
                for subscription in packet.subscriptions[:MAX_RPC_ITEMS]:
                    self.handle_subscription(peer, subscription)
                for message in packet.publish[:32]:
                    if not self._is_subscribed_to_msg(message):
                        continue
                    try:
                        self._message_slots.acquire_nowait()
                    except trio.WouldBlock:
                        continue
                    try:
                        self.manager.run_task(self._bounded_push, peer, message)
                    except Exception:
                        self._message_slots.release()
                        return
                if packet.HasField("control"):
                    await self.router.handle_rpc(packet, peer)
        except (EOFError, StreamError, DecodeError, ValueError, trio.TooSlowError):
            await _reset(stream)
        finally:
            self._inbound_count -= 1
            self._inbound[peer] -= 1
            if not self._inbound[peer]:
                self._inbound.pop(peer)
            if self._incoming_epoch.get(peer) is epoch:
                self._incoming_epoch.pop(peer)
                outbound = self.peers.get(peer)
                self._handle_dead_peer(peer)
                if outbound is not None:
                    await _reset(outbound)


class PublicPubsubService:
    """One open public transport with bounded, independent URL subscriptions.

    All methods and ``status`` belong to the host's Trio runtime. The managed
    node bridges HTTP calls to that runtime and copies its status outward.
    """

    def __init__(self, host, dht, config, addresses, *, default_network="syn"):
        # Reuse the public grammar to validate a default namespace without
        # assuming this host has a local Operator installation.
        group_descriptor(default_network, "syn://type/public-pubsub")
        self.host, self.dht, self.config, self.addresses = host, dht, config, addresses
        self.default_network = default_network
        self._groups, self._topics = {}, {}
        self._mutation = trio.Lock()
        self._dial_slots = trio.Semaphore(3)
        self._dialing = set()
        self._buffer_order = OrderedDict()
        self._buffered_bytes = 0
        self._maintenance_count = 0
        self._nursery = None
        self._running = False
        # Stay on the existing v1.1 wire profile during the library migration.
        # Upstream 0.8.1 rate-limits *locally published* messages to 10/s by
        # default, which would silently truncate Syn's bounded burst delivery.
        # Preserve a finite 512/s cap and deterministic legacy mesh geometry;
        # enabling v1.2/2.0 and their adaptive limits is a separate milestone.
        self.router = _PublicRouter([PROTOCOL_ID_V11], degree=6, degree_low=4,
            degree_high=12, heartbeat_interval=1, do_px=False,
            adaptive_gossip_enabled=False, eclipse_protection_enabled=False,
            max_messages_per_topic_per_second=512.0)
        self.pubsub = _PublicPubsub(host, self.router, self)

    def _available(self):
        if not self._running or self._nursery is None:
            raise PublicPubsubUnavailable("Public GossipSub is not running")

    def _current(self, group):
        return self._running and self._groups.get(group.key) is group

    def _group(self, key):
        self._available()
        if type(key) is not str or _GROUP_HASH.fullmatch(key) is None:
            raise ValueError("Expected a lowercase public group hash")
        try:
            return self._groups[key]
        except KeyError:
            raise PublicGroupNotJoined("Public group is not joined") from None

    def _group_status(self, group):
        peers = sorted(str(peer) for peer in self.pubsub.peer_topics.get(group.topic, ())
                       if peer in self.pubsub.peers)
        mesh = sorted(str(peer) for peer in self.router.mesh.get(group.topic, ()))
        return {**group.descriptor, "joined": True, "stream_id": group.stream_id,
                "cursor": group.cursor(), "buffered": len(group.history), "dropped": group.dropped,
                "published": group.published, "received": group.received,
                "peers": peers, "peer_count": len(peers), "mesh_peers": mesh,
                "mesh_peer_count": len(mesh), "discovery": dict(group.discovery)}

    def status(self):
        return {"enabled": True, "running": self._running,
                "default_network_id": self.default_network,
                "groups": [self._group_status(group) for group in self._groups.values()],
                "limits": {"groups": self.config.publicPubsubMaxGroups,
                    "messages_per_group": self.config.publicPubsubQueueSize,
                    "message_bytes": MAX_MESSAGE_BYTES, "buffer_bytes": MAX_BUFFER_BYTES}}

    async def run(self, *, task_status=trio.TASK_STATUS_IGNORED):
        if self._running:
            raise RuntimeError("Public GossipSub service is already running")
        try:
            async with (background_trio_service(self.pubsub), background_trio_service(self.router)):
                await self.pubsub.wait_until_ready()
                async with trio.open_nursery() as nursery:
                    self._nursery = nursery
                    self._running = True
                    nursery.start_soon(self._discovery_loop)
                    task_status.started()
                    await trio.sleep_forever()
        finally:
            self._running = False
            self._nursery = None
            self._groups.clear()
            self._topics.clear()
            self._buffer_order.clear()
            self._buffered_bytes = 0
            self._maintenance_count = 0
            self.pubsub._retry.clear()

    async def join(self, url, network_id=None):
        self._available()
        descriptor = group_descriptor(self.default_network if network_id is None else network_id, url)
        async with self._mutation:
            self._available()
            if descriptor["group_hash"] in self._groups:
                return self._group_status(self._groups[descriptor["group_hash"]])
            if len(self._groups) >= self.config.publicPubsubMaxGroups:
                raise PublicPubsubBusy("Public group subscription capacity is exhausted")
            group = _Group(descriptor)
            if self.dht is None:
                group.discovery.update(state="disabled", publication="disabled")
            self._groups[group.key], self._topics[group.topic] = group, group
            # Reserve a metadata entry for our own topic even if remote-only
            # announcements have filled the bounded discovery metadata table.
            if group.topic not in self.pubsub.peer_topics:
                if len(self.pubsub.peer_topics) >= MAX_REMOTE_TOPICS:
                    for old in list(self.pubsub.peer_topics):
                        if old not in self._topics:
                            self.pubsub.peer_topics.pop(old)
                            break
                self.pubsub.peer_topics.setdefault(group.topic, set())
            try:
                subscription = await self.pubsub.subscribe(group.topic)
                self._nursery.start_soon(self._reader, group, subscription)
                # An inbound public participant may have announced this topic
                # before we joined. It needs no identity admission or DHT hit.
                for peer in tuple(self.pubsub.peer_topics.get(group.topic, ())):
                    if self.pubsub._inbound.get(peer, 0):
                        self.pubsub.manager.run_task(self.pubsub._handle_new_peer_safe, peer)
            except BaseException:
                self._groups.pop(group.key, None)
                self._topics.pop(group.topic, None)
                await self._unsubscribe(group)
                raise
            return self._group_status(group)

    async def _unsubscribe(self, group):
        try:
            with trio.move_on_after(1, shield=True):
                await self.pubsub.unsubscribe(group.topic)
        finally:
            # A cancelled announcement must not retain local forwarding state.
            self.router.mesh.pop(group.topic, None)
            self.router.fanout.pop(group.topic, None)
            self.router.time_since_last_publish.pop(group.topic, None)
            self.pubsub.remove_topic_validator(group.topic)

    async def leave(self, key):
        self._available()
        if type(key) is not str or _GROUP_HASH.fullmatch(key) is None:
            raise ValueError("Expected a lowercase public group hash")
        async with self._mutation:
            group = self._groups.pop(key, None)
            if group is None:
                return {"group_hash": key, "joined": False, "already_left": True}
            self._topics.pop(group.topic, None)
            if group.reader_scope is not None:
                group.reader_scope.cancel()
            if group.discovery_scope is not None:
                group.discovery_scope.cancel()
            while group.history:
                self._drop_oldest(group)
            group.seen.clear()
            group.providers.clear()
            # Host notifications launch opens independently of discovery's
            # cancel scope. Stop those tasks through the initial hello write;
            # they do not enter pubsub.peers until that write has completed.
            for peer, selected in tuple(self.pubsub._opening.items()):
                if not self.pubsub.selected_groups(peer, selected):
                    scope = self.pubsub._opening_scopes.get(peer)
                    if scope is not None:
                        scope.cancel()
            await self._unsubscribe(group)
            # Release unused pubsub streams only. DHT connectivity on the same
            # host is not stopped when a group is left.
            for peer, stream in tuple(self.pubsub.peers.items()):
                if not self.pubsub.selected_groups(peer):
                    self.pubsub._handle_dead_peer(peer)
                    await _reset(stream)
            return {"group_hash": key, "joined": False, "already_left": False}

    async def publish(self, key, data):
        async with self._mutation:
            group = self._group(key)
            encoded = encode_message(group.descriptor["network_id"], group.descriptor["url"], data)
            packet = decode_message(encoded, network_id=group.descriptor["network_id"],
                                    url=group.descriptor["url"])
            await self.pubsub.publish(group.topic, encoded)
            if not self._current(group):
                raise PublicGroupNotJoined("Public group left during publication")
            group.published += 1
            return {"group_hash": key, "message_id": packet["message_id"],
                    "sender_peer_id": str(self.host.get_id()), "sent_at": packet["sent_at"],
                    "accepted": True, "peer_reception_proved": False}

    async def read(self, key, after=None, limit=100):
        group = self._group(key)
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError("Public message limit must be between 1 and 256")
        offset = 0
        if after is not None:
            match = _CURSOR.fullmatch(after) if type(after) is str else None
            if match is None:
                raise ValueError("Malformed public message cursor")
            if match[1] != group.stream_id:
                raise PublicCursorExpired("Public group subscription was replaced; obtain a new cursor")
            offset = int(match[2])
            if offset > group.sequence:
                raise ValueError("Public message cursor is ahead of this subscription")
        first = group.history[0][0] if group.history else group.sequence + 1
        dropped = max(0, first - offset - 1)
        messages = [packet for sequence, _, packet in group.history if sequence > offset][:limit]
        cursor = messages[-1]["cursor"] if messages else group.cursor()
        return {"group_hash": key, "stream_id": group.stream_id, "messages": deepcopy(messages),
                "cursor": cursor, "dropped": dropped,
                "has_more": bool(messages and int(cursor.rsplit(":", 1)[1]) < group.sequence)}

    def _drop_oldest(self, group):
        sequence, size, _ = group.history.popleft()
        self._buffer_order.pop((group, sequence), None)
        self._buffered_bytes -= size
        group.dropped += 1

    def _retain(self, group, sender, packet, size):
        identity = (sender, packet["message_id"])
        now = time.monotonic()
        while group.seen and next(iter(group.seen.values())) < now - 300:
            group.seen.popitem(last=False)
        if identity in group.seen:
            return
        group.seen[identity] = now
        while len(group.seen) > max(256, self.config.publicPubsubQueueSize * 2):
            group.seen.popitem(last=False)
        group.sequence += 1
        group.received += 1
        message = {**packet, "sender_peer_id": sender, "received_at": int(time.time()),
                   "cursor": group.cursor()}
        group.history.append((group.sequence, size, message))
        self._buffer_order[group, group.sequence] = None
        self._buffered_bytes += size
        while len(group.history) > self.config.publicPubsubQueueSize:
            self._drop_oldest(group)
        while self._buffered_bytes > MAX_BUFFER_BYTES:
            owner, _ = next(iter(self._buffer_order))
            self._drop_oldest(owner)

    async def _reader(self, group, subscription):
        with trio.CancelScope() as scope:
            group.reader_scope = scope
            try:
                while self._current(group):
                    message = await subscription.get()
                    if not self._current(group):
                        return
                    try:
                        packet = decode_message(message.data, network_id=group.descriptor["network_id"],
                                                url=group.descriptor["url"])
                    except (PublicPubsubError, ValueError, TypeError):
                        continue
                    self._retain(group, str(ID(message.from_id)), packet, len(message.data))
            except (trio.EndOfChannel, trio.ClosedResourceError):
                return
            finally:
                group.reader_scope = None

    async def _discovery_loop(self):
        if self.dht is None:
            await trio.sleep_forever()
        while True:
            # At most two maintenance jobs contend with the host's shared DHT
            # lock. Oldest-due selection gives every joined group a turn.
            capacity = 2 - self._maintenance_count
            due = sorted((group for group in self._groups.values()
                          if not group.pending and group.next_lookup <= trio.current_time()),
                         key=lambda group: group.next_lookup)
            for group in due[:max(0, capacity)]:
                group.pending = True
                self._maintenance_count += 1
                self._nursery.start_soon(self._maintain, group)
            await trio.sleep(.1)

    async def _maintain(self, group):
        try:
            with trio.CancelScope() as scope:
                group.discovery_scope = scope
                if not self._current(group):
                    return
                with trio.fail_after(min(10, self.config.lookupTimeoutSeconds)):
                    addresses = self.addresses()
                    current = tuple(sorted(str(address) for address in addresses))
                    if current != group.previous_addresses:
                        group.previous_addresses, group.next_announcement = current, 0
                    if not addresses:
                        group.discovery["publication"] = "waiting-addresses"
                    elif trio.current_time() >= group.next_announcement:
                        sent = await self.dht.provide(bytes.fromhex(group.descriptor["provider_key"]), addresses)
                        if not self._current(group):
                            return
                        group.discovery["announcements_sent"] += sent
                        group.discovery["publication"] = "announced" if sent else "unavailable"
                        delay = self.config.publicPubsubProviderRepublishSeconds if sent else group.announce_retry
                        group.next_announcement = trio.current_time() + delay
                        group.announce_retry = 1 if sent else min(300, group.announce_retry * 2)
                    candidates = await self.dht.find_providers(bytes.fromhex(group.descriptor["provider_key"]), count=20)
                    if not self._current(group):
                        return
                    available = getattr(self.dht, "status", lambda: {})().get("lastLookupStatus") != "unavailable"
                    if available:
                        expires = trio.current_time() + max(30, self.config.publicPubsubDiscoveryIntervalSeconds * 3)
                        group.providers = {candidate.peer_id: expires for candidate in candidates[:20]
                                           if candidate.peer_id != self.host.get_id()}
                    group.discovery.update(state="ready" if available else "unavailable",
                        candidates=min(20, len(candidates)),
                        last_error=None if available else "No responding DHT servers")
                    async with trio.open_nursery() as nursery:
                        for candidate in candidates[:20]:
                            nursery.start_soon(self._connect, group, candidate)
        except Exception as error:
            if self._current(group):
                group.discovery.update(state="unavailable", last_error=type(error).__name__)
        finally:
            group.discovery_scope = None
            group.pending = False
            self._maintenance_count -= 1
            group.next_lookup = trio.current_time() + self.config.publicPubsubDiscoveryIntervalSeconds

    async def _connect(self, group, candidate):
        peer = candidate.peer_id
        if (peer == self.host.get_id() or peer in self._dialing
                or peer in self.pubsub.peers
                or not self._current(group) or peer not in group.providers
                or not self.pubsub.can_attempt(peer)):
            return
        self._dialing.add(peer)
        try:
            async with self._dial_slots:
                if not self._current(group) or not self.pubsub.can_attempt(peer):
                    return
                with trio.move_on_after(self.config.requestTimeoutSeconds):
                    candidate = seed_dial_addresses(self.host, candidate,
                        allow_private=self.config.allowPrivateAddresses)
                    await self.host.connect(candidate)
                    if not self._current(group):
                        return
                    # An existing DHT connection may predate this Pubsub
                    # service or its peer's readiness; retry stream negotiation.
                    await self.pubsub._handle_new_peer_safe(peer)
                # A transport timeout can occur before pubsub has had a chance
                # to install its own retry delay. Avoid redialing that provider
                # on every group-discovery tick as well.
                if (peer not in self.pubsub.peers and peer not in self.pubsub._opening
                        and self.pubsub.can_attempt(peer)):
                    self.pubsub.failed_attempt(peer)
        except Exception:
            if self._current(group) and self.pubsub.selected_groups(peer):
                self.pubsub.failed_attempt(peer)
            return
        finally:
            self._dialing.discard(peer)
