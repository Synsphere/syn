"""Discover a GossipSub group's providers and authenticate their native Agents."""
from __future__ import annotations

from collections import deque
import hashlib
import math
from pathlib import Path
import threading

import multiaddr
import trio
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.crypto.x25519 import create_new_key_pair as noise_key_pair
from libp2p.peer.id import ID
from libp2p.peer.peerinfo import PeerInfo
from libp2p.pubsub.gossipsub import PROTOCOL_ID_V11, GossipSub
from libp2p.security.noise.transport import Transport as NoiseTransport
from libp2p.stream_muxer.yamux.yamux import Yamux
from libp2p.tools.anyio_service import background_trio_service

from syn.mesh import PrivateRelay
from syn.amino import AminoDHTClient, dialable_tcp_addresses, seed_dial_addresses
from syn.config import P2PConfig
from syn.mesh_pubsub import AuthenticatedPubsub
from syn.operator.custody import private_read
from syn.p2p_defaults import IPFS_BOOTSTRAP_PEERS
from syn.peer_record_transport import read_frame, write_frame
from syn_client.operator_protocol import MAX_FRAME_BYTES, SynProtocolError, canonical, exact, hex_bytes, parse, receive, send
from syn_client.pubsub import MAX_PEERS, MAX_QUEUE, group_hash, scope, verify_hello, verify_message, verify_relay_proof


PROFILE = "syn.private-gossipsub-config/2"
LEGACY_PROFILE = "syn.private-gossipsub-config/1"
AUTH_PROTOCOL = "/syn/private-gossipsub/auth/1.0.0"
AUTH_PROFILE = "syn.private-gossipsub-auth/1"
STARTUP_SECONDS = 30


def _cancellation_only(error):
    """Nested Trio service nurseries may group an otherwise clean shutdown."""
    return isinstance(error, trio.Cancelled) or (
        isinstance(error, BaseExceptionGroup)
        and all(_cancellation_only(child) for child in error.exceptions)
    )


def _discovery_settings(value, listen):
    fields = {"bootstrapPeers", "announceAddresses", "allowPrivateAddresses",
              "requestTimeoutSeconds", "lookupTimeoutSeconds", "bootstrapRefreshSeconds",
              "providerRepublishSeconds", "discoveryIntervalSeconds"}
    if type(value) is not dict or set(value) - fields:
        raise SynProtocolError("Invalid GossipSub discovery settings")
    for name in ("bootstrapPeers", "announceAddresses"):
        if name in value and type(value[name]) is not list:
            raise SynProtocolError("GossipSub discovery addresses must be arrays")
    interval = value.get("discoveryIntervalSeconds", 3)
    if type(interval) not in (int, float) or not math.isfinite(interval) or not 1 <= interval <= 30:
        raise SynProtocolError("GossipSub discovery interval must be between 1 and 30 seconds")
    try:
        settings = P2PConfig(mdnsEnabled=False, upnpEnabled=False, publishOperatorRecord=False,
            listenAddresses=(str(listen),),
            bootstrapPeers=tuple(value.get("bootstrapPeers", IPFS_BOOTSTRAP_PEERS)),
            announceAddresses=tuple(value.get("announceAddresses", [])),
            allowPrivateAddresses=value.get("allowPrivateAddresses", False),
            requestTimeoutSeconds=value.get("requestTimeoutSeconds", 3),
            lookupTimeoutSeconds=value.get("lookupTimeoutSeconds", 10),
            bootstrapRefreshSeconds=value.get("bootstrapRefreshSeconds", 600),
            providerRepublishSeconds=value.get("providerRepublishSeconds", 3 * 60 * 60))
    except (TypeError, ValueError) as error:
        raise SynProtocolError("Invalid GossipSub discovery settings: " + str(error)) from None
    return settings, float(interval)


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
        self.dynamic = type(config) is dict and config.get("profile") == PROFILE
        fields = {"profile", "scope", "endpoints", "identity_path", "listen"}
        if self.dynamic:
            if "discovery" in config:
                fields.add("discovery")
        else:
            fields.add("peers")
        config = exact(config, fields, "GossipSub relay configuration")
        if config["profile"] not in {PROFILE, LEGACY_PROFILE} or not isinstance(config["identity_path"], str):
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
        if self.dynamic:
            selected = scope(config["scope"])
            if type(config["endpoints"]) is not list or any(type(e) is not dict for e in config["endpoints"]):
                raise SynProtocolError("Invalid local GossipSub Agent endpoints")
            if any(type(entry.get("node")) is not str for entry in config["endpoints"]):
                raise SynProtocolError("GossipSub local Agent identifiers must be strings")
            local_nodes = {entry["node"] for entry in config["endpoints"]}
            remote_nodes = {cert["body"]["node_public_key_hash"] for cert in selected["members"]} - local_nodes
            self.discovery_config, self.discovery_interval = _discovery_settings(config.get("discovery", {}), self.listen)
            rows = []
        else:
            rows = config["peers"]
            if not isinstance(rows, list) or not 1 <= len(rows) < MAX_PEERS:
                raise SynProtocolError("Invalid number of selected GossipSub peers")
            self.discovery_config = None
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
        self.local_nodes = frozenset(entry["node"] for entry in self.endpoints)
        self.remote_nodes = frozenset(remote_nodes)
        self.group_hash = group_hash(self.selected)
        self.provider_key = b"\x12\x20" + bytes.fromhex(self.group_hash)
        self.topic = "/syn/private-pubsub/1/" + self.group_hash
        self.expected = len(remote_nodes) + len(self.endpoints)
        self.ready_peers = {}
        self.early = deque(maxlen=MAX_QUEUE)
        self.active = False
        self.proofs = {}
        self.host = self.dht = None
        self._auth_slots = trio.Semaphore(MAX_PEERS)
        self._auth_pending = set()
        self._next_bootstrap = 0.0
        self._discovery_status = {"enabled": self.dynamic, "groupHash": self.group_hash,
            "providerKey": self.provider_key.hex(), "state": "stopped", "announcementsSent": 0,
            "publication": "waiting-addresses" if self.dynamic else "disabled",
            "candidates": 0, "authenticatedPeers": 0, "rejectedCandidates": 0,
            "lastError": None}

    def discovery_status(self):
        return {**self._discovery_status, "authenticatedPeers": len(self.peers),
                "amino": self.dht.status() if self.dht is not None else None}

    def _attach_local(self):
        if not self.dynamic:
            return super()._attach_local()
        from syn_client.pubsub_client import private_connect
        for entry in self.endpoints:
            node = entry["node"]
            conn = private_connect(Path(entry["socket"]), entry["owner_uid"])
            self.connections[node] = conn
            self.writes[node] = threading.Lock()
            send(conn, {"profile": "syn.private-pubsub-link/2", "scope": self.selected,
                        "relay_public_key": self.key_pair.public_key.to_bytes().hex()})
            proof = receive(conn)
            hello = verify_relay_proof(proof, self.selected, expected_peer_id=str(self.identity))
            if hello["body"]["node"] != node:
                raise SynProtocolError("Native relay proof belongs to another local Agent")
            self.proofs[node], self.hellos[node] = proof, hello

    def _auth_packet(self):
        return {"profile": AUTH_PROFILE, "group_hash": self.group_hash,
                "proofs": [self.proofs[node] for node in sorted(self.local_nodes)]}

    def _accept_proofs(self, peer, packet):
        packet = exact(packet, {"profile", "group_hash", "proofs"}, "GossipSub group authentication")
        if (peer == self.identity or packet["profile"] != AUTH_PROFILE
                or packet["group_hash"] != self.group_hash
                or type(packet["proofs"]) is not list or not 1 <= len(packet["proofs"]) < MAX_PEERS
                or (self.active and peer not in self.peers)):
            raise SynProtocolError("GossipSub provider differs from the selected group or live session")
        verified = {}
        for proof in packet["proofs"]:
            hello = verify_relay_proof(proof, self.selected, expected_peer_id=str(peer))
            node = hello["body"]["node"]
            if node not in self.remote_nodes or node in verified:
                raise SynProtocolError("GossipSub provider claims a duplicate or local Agent")
            verified[node] = hello
        assigned = frozenset(verified)
        if peer in self.peers and self.peers[peer] != assigned:
            raise SynProtocolError("GossipSub provider changed its native Agent assignment")
        if any(nodes & assigned for owner, nodes in self.peers.items() if owner != peer):
            raise SynProtocolError("GossipSub Agent is assigned to more than one authenticated provider")
        if any(node in self.hellos and self.hellos[node] != hello for node, hello in verified.items()):
            raise SynProtocolError("GossipSub provider changed its signed live roster")
        # Verify the entire claim before publishing any of its assignments.
        self.peers[peer] = assigned
        self.hellos.update(verified)

    async def _authenticate_stream(self, stream):
        acquired = False
        try:
            self._auth_slots.acquire_nowait()
            acquired = True
            with trio.fail_after(self.discovery_config.requestTimeoutSeconds):
                packet = parse(await read_frame(stream, MAX_FRAME_BYTES))
                peer = stream.muxed_conn.peer_id
                self._accept_proofs(peer, packet)
                await self.pubsub.wait_until_ready()
                await self.pubsub.admit(peer)
                await write_frame(stream, canonical(self._auth_packet()), MAX_FRAME_BYTES)
        except Exception:
            self._discovery_status["rejectedCandidates"] += 1
        finally:
            try:
                with trio.move_on_after(1, shield=True):
                    try:
                        await stream.close()
                    except Exception:
                        pass
            finally:
                if acquired:
                    self._auth_slots.release()

    async def _authenticate_candidate(self, candidate):
        peer = candidate.peer_id
        if peer == self.identity or peer in self._auth_pending or self.active:
            return
        self._auth_pending.add(peer)
        stream = None
        try:
            with trio.fail_after(self.discovery_config.requestTimeoutSeconds):
                candidate = seed_dial_addresses(self.host, candidate,
                    allow_private=self.discovery_config.allowPrivateAddresses)
                await self.host.connect(candidate)
                stream = await self.host.new_stream(peer, [AUTH_PROTOCOL])
                if stream.muxed_conn.peer_id != peer:
                    raise SynProtocolError("GossipSub provider transport identity changed")
                await write_frame(stream, canonical(self._auth_packet()), MAX_FRAME_BYTES)
                packet = parse(await read_frame(stream, MAX_FRAME_BYTES))
                self._accept_proofs(peer, packet)
                await self.pubsub.admit(peer)
        except Exception:
            self._discovery_status["rejectedCandidates"] += 1
        finally:
            try:
                if stream is not None:
                    with trio.move_on_after(1, shield=True):
                        try:
                            await stream.close()
                        except Exception:
                            pass
            finally:
                self._auth_pending.discard(peer)

    async def _discover(self):
        settings = self.discovery_config
        next_bootstrap, next_announcement = self._next_bootstrap, 0.0
        bootstrap_retry = announce_retry = self.discovery_interval
        previous_addresses = ()
        while not self.stopped.is_set():
            now = trio.current_time()
            if now >= next_bootstrap:
                try:
                    with trio.fail_after(settings.lookupTimeoutSeconds):
                        count = await self.dht.bootstrap()
                    self._discovery_status.update(state="ready" if count else "unavailable", lastError=None)
                    next_bootstrap = trio.current_time() + (settings.bootstrapRefreshSeconds if count else bootstrap_retry)
                    bootstrap_retry = self.discovery_interval if count else min(bootstrap_retry * 2, 300)
                except Exception as error:
                    self._discovery_status.update(state="unavailable", lastError=type(error).__name__)
                    next_bootstrap = trio.current_time() + bootstrap_retry
                    bootstrap_retry = min(bootstrap_retry * 2, 300)
            addresses = dialable_tcp_addresses(self.host.get_addrs(), peer_id=self.identity,
                allow_private=settings.allowPrivateAddresses)
            current_addresses = tuple(sorted(str(a) for a in addresses))
            if current_addresses != previous_addresses:
                previous_addresses, next_announcement = current_addresses, 0.0
            if not addresses:
                self._discovery_status["publication"] = "waiting-addresses"
            if addresses and self.dht.status()["routingPeers"] and trio.current_time() >= next_announcement:
                try:
                    with trio.fail_after(settings.lookupTimeoutSeconds):
                        sent = await self.dht.provide(self.provider_key, addresses)
                    self._discovery_status["announcementsSent"] += sent
                    self._discovery_status["publication"] = "announced" if sent else "unavailable"
                    next_announcement = trio.current_time() + (settings.providerRepublishSeconds if sent else announce_retry)
                    announce_retry = self.discovery_interval if sent else min(announce_retry * 2, 300)
                except Exception as error:
                    self._discovery_status["lastError"] = type(error).__name__
                    self._discovery_status["publication"] = "unavailable"
                    next_announcement = trio.current_time() + announce_retry
                    announce_retry = min(announce_retry * 2, 300)
            if not self.active:
                try:
                    with trio.fail_after(settings.lookupTimeoutSeconds):
                        candidates = await self.dht.find_providers(self.provider_key)
                        self._discovery_status["candidates"] = len(candidates)
                        slots = trio.Semaphore(3)
                        async def authenticate(candidate):
                            async with slots:
                                await self._authenticate_candidate(candidate)
                        async with trio.open_nursery() as nursery:
                            for candidate in candidates:
                                nursery.start_soon(authenticate, candidate)
                except Exception as error:
                    self._discovery_status["lastError"] = type(error).__name__
            await trio.sleep(self.discovery_interval)

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
        async with trio.open_nursery() as lifetime:
            async def stop_when_requested():
                while not self.stopped.is_set():
                    await trio.sleep(0.05)
                lifetime.cancel_scope.cancel()
            lifetime.start_soon(stop_when_requested)
            await self._run_host(on_ready)
            lifetime.cancel_scope.cancel()

    async def _run_host(self, on_ready):
        try:
            if not self.dynamic:
                await trio.to_thread.run_sync(self._attach_local)
            self.token = trio.lowlevel.current_trio_token()
            settings = self.discovery_config
            host = new_host(key_pair=self.key_pair, bootstrap=[], enable_mDNS=False, enable_upnp=False,
                announce_addrs=([multiaddr.Multiaddr(a) for a in settings.announceAddresses]
                                if settings and settings.announceAddresses else None),
                sec_opt={"/noise": NoiseTransport(self.key_pair, noise_key_pair().private_key)},
                muxer_opt={"/yamux/1.0.0": Yamux})
            self.host = host
            router = GossipSub([PROTOCOL_ID_V11], degree=MAX_PEERS, degree_low=1,
                               degree_high=MAX_PEERS, heartbeat_interval=1,
                               adaptive_gossip_enabled=False, eclipse_protection_enabled=False,
                               max_messages_per_topic_per_second=128.0)
            self.pubsub = AuthenticatedPubsub(host, router, strict_signing=True,
                                             admitted_peers=tuple(self.peers))
            self.pubsub.set_topic_validator(self.topic, self._validate, False)
            if self.dynamic:
                self.dht = AminoDHTClient(host, settings.bootstrapPeers,
                    timeout=settings.requestTimeoutSeconds, allow_private=settings.allowPrivateAddresses)
                host.set_stream_handler(AUTH_PROTOCOL, self._authenticate_stream)
            async with (host.run(listen_addrs=[self.listen]),
                        background_trio_service(self.pubsub), background_trio_service(router)):
                await self.pubsub.wait_until_ready()
                if self.dynamic:
                    # Warm routing before issuing short-lived native proofs.
                    # Unavailable public bootstrap remains a bounded retry;
                    # native cohort readiness retains its own hard deadline.
                    self._discovery_status["state"] = "bootstrapping"
                    with trio.move_on_after(settings.lookupTimeoutSeconds):
                        count = await self.dht.bootstrap()
                        self._discovery_status["state"] = "ready" if count else "unavailable"
                        if count:
                            self._next_bootstrap = trio.current_time() + settings.bootstrapRefreshSeconds
                    if self.stopped.is_set():
                        return
                    await trio.to_thread.run_sync(self._attach_local)
                subscription = await self.pubsub.subscribe(self.topic)
                async with trio.open_nursery() as nursery:
                    if self.dynamic:
                        nursery.start_soon(self._discover)
                    await self._serve(host, subscription, on_ready)
                    nursery.cancel_scope.cancel()
        except BaseException as error:
            if not (self.stopped.is_set() and _cancellation_only(error)):
                self.fault.set()
            raise
        finally:
            self._discovery_status["state"] = "stopped"
            with trio.CancelScope(shield=True):
                await trio.to_thread.run_sync(self.close)

    async def _serve(self, host, subscription, on_ready):
        deadline = trio.current_time() + STARTUP_SECONDS
        next_publish = 0
        local_hellos = canonical({"kind": "hello", "hellos": [self.hellos[node] for node in sorted(self.local_nodes)]})
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
                        if (not self.active and self.peers.keys() <= subscribed
                                and all(self.ready_peers.get(peer) == digest for peer in self.peers)):
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
            # Once dequeued, a message must outlive the idle poll deadline.
            with trio.move_on_after(0.1) as poll:
                message = await subscription.get()
            if not poll.cancelled_caught:
                await self._receive(message)
