"""Carry the native Agent relay's signed envelopes over a pinned GossipSub mesh."""
from __future__ import annotations

from collections import deque
import hashlib
from pathlib import Path

import multiaddr
import trio
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.peer.id import ID
from libp2p.peer.peerinfo import PeerInfo
from libp2p.pubsub.gossipsub import PROTOCOL_ID_V11, GossipSub
from libp2p.pubsub.pubsub import Pubsub
from libp2p.tools.anyio_service import background_trio_service

from syn.mesh import PrivateRelay
from syn.operator.custody import private_read
from syn_client.operator_protocol import MAX_FRAME_BYTES, SynProtocolError, canonical, exact, hex_bytes, parse
from syn_client.pubsub import MAX_PEERS, MAX_QUEUE, verify_hello, verify_message


PROFILE = "syn.private-gossipsub-config/1"
STARTUP_SECONDS = 30


def _identity(path):
    raw = private_read(path, 32)
    if len(raw) != 32:
        raise SynProtocolError("GossipSub relay identity requires a private 32-byte seed")
    return create_new_key_pair(raw)


def peer_id(path):
    return str(ID.from_pubkey(_identity(path).public_key))


def _tcp_address(value, *, listen=False):
    try:
        address = multiaddr.Multiaddr(value)
        protocols = tuple(protocol.name for protocol in address.protocols())
        if protocols not in {("ip4", "tcp"), ("ip6", "tcp")} or str(address) != value:
            raise ValueError
        if not listen and int(address.value_for_protocol("tcp")) == 0:
            raise ValueError
    except (TypeError, ValueError):
        raise SynProtocolError("GossipSub relay requires a canonical IP/TCP address") from None
    return address


class GossipRelay(PrivateRelay):
    def __init__(self, config):
        config = exact(config, {"profile", "scope", "endpoints", "identity_path", "listen", "peers"},
                       "GossipSub relay configuration")
        if config["profile"] != PROFILE or not isinstance(config["identity_path"], str):
            raise SynProtocolError("Invalid GossipSub relay profile or identity path")
        path = Path(config["identity_path"])
        if not path.is_absolute() or path.resolve() != path:
            raise SynProtocolError("GossipSub relay identity requires a canonical absolute path")
        self.key_pair = _identity(path)
        self.identity = ID.from_pubkey(self.key_pair.public_key)
        self.listen = _tcp_address(config["listen"], listen=True)
        self.peers = {}
        self.dial = []
        remote_nodes = set()
        rows = config["peers"]
        if not isinstance(rows, list) or not 1 <= len(rows) < MAX_PEERS:
            raise SynProtocolError("Invalid number of selected GossipSub peers")
        for row in rows:
            row = exact(row, {"peer_id", "address", "nodes"}, "GossipSub relay peer")
            try:
                peer = ID.from_string(row["peer_id"])
            except (TypeError, ValueError, AttributeError):
                raise SynProtocolError("Invalid GossipSub peer identity") from None
            if str(peer) != row["peer_id"] or peer == self.identity or peer in self.peers:
                raise SynProtocolError("GossipSub peer identity is duplicate or invalid")
            nodes = row["nodes"]
            if not isinstance(nodes, list) or not 1 <= len(nodes) < MAX_PEERS:
                raise SynProtocolError("GossipSub peer needs an explicit Agent roster")
            nodes = [hex_bytes(node, 32, "GossipSub relay Agent").hex() for node in nodes]
            if len(set(nodes)) != len(nodes) or remote_nodes.intersection(nodes):
                raise SynProtocolError("GossipSub Agent is assigned to more than one peer")
            self.peers[peer] = frozenset(nodes)
            remote_nodes.update(nodes)
            if row["address"] is not None:
                self.dial.append(PeerInfo(peer, [_tcp_address(row["address"])]))

        super().__init__({"profile": "syn.private-relay-config/1", "scope": config["scope"],
                          "endpoints": config["endpoints"]}, remote_nodes=remote_nodes)
        roster = sorted(self.selected["members"], key=lambda cert: cert["body"]["node_public_key_hash"])
        self.topic = "/syn/private-pubsub/1/" + hashlib.sha256(
            canonical({**self.selected, "members": roster})
        ).hexdigest()
        self.expected = len(remote_nodes) + len(self.endpoints)
        self.ready_peers = {}
        self.early = deque(maxlen=MAX_QUEUE)
        self.active = False

    def _forward(self, node, packet):
        trio.from_thread.run(self.pubsub.publish, self.topic, canonical(packet), trio_token=self.token)
        super()._forward(node, packet)

    def _validate(self, forwarder, message):
        return (forwarder in self.peers or forwarder == self.identity) and (
            ID(message.from_id) in self.peers or ID(message.from_id) == self.identity
        ) and 0 < len(message.data) <= MAX_FRAME_BYTES

    def _roster_digest(self):
        return hashlib.sha256(canonical([self.hellos[node] for node in sorted(self.hellos)])).hexdigest()

    def _receive_hello(self, peer, packet):
        hellos = exact(packet, {"kind", "hellos"}, "GossipSub relay roster")["hellos"]
        assigned = self.peers[peer]
        if not isinstance(hellos, list) or len(hellos) != len(assigned):
            raise SynProtocolError("GossipSub hello list differs from the peer assignment")
        verified = {}
        for hello in hellos:
            node = hello.get("body", {}).get("node")
            if node not in assigned or node in verified:
                raise SynProtocolError("GossipSub hello sender differs from the peer assignment")
            verify_hello(hello, self.selected, expected_node=node)
            verified[node] = hello
        if any(node in self.hellos and self.hellos[node] != hello for node, hello in verified.items()):
            raise SynProtocolError("GossipSub peer changed its signed live roster")
        self.hellos.update(verified)

    def _receive_envelope(self, peer, packet):
        envelope = exact(packet, {"kind", "envelope"}, "GossipSub publication")["envelope"]
        node = envelope.get("header", {}).get("node")
        if node not in self.peers[peer] or node not in self.hellos:
            raise SynProtocolError("GossipSub sender lacks an assigned live Agent identity")
        header = verify_message(envelope, self.selected, expected_node=node,
                                expected_boot=self.hellos[node]["body"]["boot_id"])
        if header["sequence"] <= self.sequences.get(node, 0):
            return
        self.sequences[node] = header["sequence"]
        super()._forward(node, packet)

    async def _receive(self, message):
        peer = ID(message.from_id)
        if peer not in self.peers:
            return
        try:
            packet = parse(message.data)
            kind = packet.get("kind")
            if kind == "message":
                if self.active:
                    await trio.to_thread.run_sync(self._receive_envelope, peer, packet)
                else:
                    self.early.append((peer, packet))
            elif not self.active and kind == "hello":
                self._receive_hello(peer, packet)
            elif not self.active and kind == "ready":
                digest = exact(packet, {"kind", "roster_sha256"}, "GossipSub relay readiness")["roster_sha256"]
                self.ready_peers[peer] = hex_bytes(digest, 32, "GossipSub roster digest").hex()
        except (SynProtocolError, AttributeError, KeyError, TypeError, ValueError):
            # IP transport may replay, reorder or supply invalid frames. Only
            # verified live envelopes enter a trusted local Agent connection.
            return

    async def run(self, on_ready):
        try:
            await trio.to_thread.run_sync(self._attach_local)
            self.token = trio.lowlevel.current_trio_token()
            host = new_host(key_pair=self.key_pair, enable_mDNS=False, enable_upnp=False)
            router = GossipSub([PROTOCOL_ID_V11], degree=MAX_PEERS, degree_low=1,
                               degree_high=MAX_PEERS, heartbeat_interval=1)
            self.pubsub = Pubsub(host, router, strict_signing=True)
            self.pubsub.set_topic_validator(self.topic, self._validate, False)
            async with (host.run(listen_addrs=[self.listen]),
                        background_trio_service(self.pubsub), background_trio_service(router)):
                await self.pubsub.wait_until_ready()
                subscription = await self.pubsub.subscribe(self.topic)
                await self._serve(host, subscription, on_ready)
        except BaseException:
            self.fault.set()
            raise
        finally:
            await trio.to_thread.run_sync(self.close)

    async def _serve(self, host, subscription, on_ready):
        deadline = trio.current_time() + STARTUP_SECONDS
        next_publish = 0
        local_hellos = canonical({"kind": "hello", "hellos": list(self.hellos.values())})
        while not self.stopped.is_set():
            now = trio.current_time()
            subscribed = self.pubsub.peer_topics.get(self.topic, set())
            if now >= next_publish:
                if not self.active:
                    if now >= deadline:
                        raise SynProtocolError("GossipSub relay roster did not become ready")
                    for info in self.dial:
                        if info.peer_id not in subscribed:
                            with trio.move_on_after(1):
                                try:
                                    await host.connect(info)
                                except Exception:
                                    # The explicitly pinned peer may be starting.
                                    # Startup has an absolute readiness deadline.
                                    pass
                elif not self.peers.keys() <= subscribed:
                    raise SynProtocolError("A selected GossipSub relay left the live mesh")

                if not self.active or now < deadline:
                    await self.pubsub.publish(self.topic, local_hellos)
                    if len(self.hellos) == self.expected:
                        digest = self._roster_digest()
                        await self.pubsub.publish(self.topic, canonical({"kind": "ready", "roster_sha256": digest}))
                        if not self.active and all(self.ready_peers.get(peer) == digest for peer in self.peers):
                            await trio.to_thread.run_sync(self._start_local)
                            self.active = True
                            while self.early:
                                peer, packet = self.early.popleft()
                                try:
                                    await trio.to_thread.run_sync(self._receive_envelope, peer, packet)
                                except (SynProtocolError, AttributeError, KeyError, TypeError, ValueError):
                                    continue
                            on_ready()
                next_publish = now + 0.5
            with trio.move_on_after(0.1):
                await self._receive(await subscription.get())
