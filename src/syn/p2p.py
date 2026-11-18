"""Syn's managed libp2p host, Amino discovery, and public URL groups.

The stable lookup name is the installed Operator's base-key hash. Transport
keys are ephemeral and never receive the Operator's private key or mesh rights.
Network maintenance stays with the live Trio host: a separate process has
neither its connections nor its transport key.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from functools import partial
import hashlib
import logging
import re
from threading import Event, Lock, Thread
import time
from typing import Any

import multiaddr
import trio
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.crypto.x25519 import create_new_key_pair as noise_key_pair
from libp2p.discovery.mdns.mdns import MDNSDiscovery
from libp2p.security.noise.transport import PROTOCOL_ID as NOISE_PROTOCOL, Transport as NoiseTransport
from libp2p.stream_muxer.yamux.yamux import PROTOCOL_ID as YAMUX_PROTOCOL, Yamux

from .amino import AminoDHTClient, dialable_tcp_addresses, seed_dial_addresses
from .config import P2PConfig
from .p2p_defaults import AMINO_PROTOCOL
from . import peer_record
from .peer_record_transport import PeerRecordService, fetch_peer_record
from .public_pubsub import PublicPubsubService, PublicPubsubBusy, PublicPubsubUnavailable
from syn_client.public_pubsub import group_descriptor


logger = logging.getLogger(__name__)


class DiscoveryUnavailable(RuntimeError):
    pass


class DiscoveryBusy(RuntimeError):
    pass


class Libp2pNode:
    """Runs py-libp2p's Trio host alongside the asyncio web application."""

    def __init__(self, config: P2PConfig, operator=None) -> None:
        self.config, self.operator = config, operator
        self._ready, self._stop = Event(), Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._error: BaseException | None = None
        self._peerId: str | None = None
        self._addresses: tuple[str, ...] = ()
        self._discoveredPeers: tuple[str, ...] = ()
        self._upnpExternalIp: str | None = None
        self._record = None
        self._token = self._startup_scope = None
        self._host = self._dht = None
        self._lookup_slots = None
        self._public = self._public_slots = None
        self._default_network = operator.config.get("network_id", "syn") if operator is not None else "syn"
        if (type(self._default_network) is not str
                or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", self._default_network) is None):
            raise ValueError("Invalid public pubsub default network")
        self._public_status = {"enabled": config.enabled and config.publicPubsubEnabled,
            "running": False, "default_network_id": self._default_network, "groups": []}
        self._mapped_ports: set[int] = set()
        self._amino = {"enabled": config.aminoEnabled, "protocol": AMINO_PROTOCOL,
                       "mode": "client", "state": "stopped", "routingPeers": 0,
                       "lastBootstrapAt": None, "lastError": None}
        publication = ("disabled" if not config.publishOperatorRecord else
                       "no-operator" if operator is None else "waiting-addresses")
        self._publication = {"enabled": config.publishOperatorRecord,
            "state": publication, "operator": getattr(operator, "expected_hash", None),
            "providerKey": None, "cid": None, "addresses": [], "expiresAt": None,
            "lastAnnouncedAt": None, "announcedTo": 0, "lastError": None}

    async def start(self) -> None:
        if not self.config.enabled:
            return
        if self._thread is not None:
            self.check()
            return
        self._ready.clear()
        self._stop.clear()
        self._error = None
        self._thread = Thread(target=self._run, name="syn-libp2p", daemon=True)
        self._thread.start()
        try:
            ready = await asyncio.to_thread(self._ready.wait, self.config.startupTimeoutSeconds)
            if not ready:
                raise TimeoutError("libp2p node did not start before its configured timeout")
            if self._error is not None:
                raise RuntimeError("libp2p node failed to start") from self._error
        except BaseException:
            await self.stop()
            raise
        logger.info("libp2p peer %s listening on %s", self._peerId, self._addresses)

    async def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        # Before listeners are ready there is no ordinary maintenance stop loop.
        if not self._ready.is_set() and self._token is not None and self._startup_scope is not None:
            try:
                self._token.run_sync_soon(self._startup_scope.cancel)
            except trio.RunFinishedError:
                pass
        await asyncio.to_thread(thread.join, self.config.startupTimeoutSeconds)
        if thread.is_alive():
            raise TimeoutError("libp2p node did not stop before its configured timeout")
        self._thread = None

    def check(self) -> None:
        """Local transport/custody failure fences readiness; WAN outages do not."""
        if self.config.enabled and (self._error is not None or self._thread is None
                                    or not self._thread.is_alive()):
            raise RuntimeError("Syn libp2p host lost readiness") from self._error

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": self.config.enabled,
                "running": self._thread is not None and self._thread.is_alive(),
                "peerId": self._peerId, "listenAddresses": list(self._addresses),
                "bootstrapPeers": list(self.config.bootstrapPeers),
                "mdnsEnabled": self.config.mdnsEnabled,
                "discoveredPeers": list(self._discoveredPeers),
                "upnpEnabled": self.config.upnpEnabled, "upnpExternalIp": self._upnpExternalIp,
                "amino": deepcopy(self._amino), "publication": deepcopy(self._publication),
                "publicPubsub": deepcopy(self._public_status)}

    async def public_groups(self) -> dict:
        """Read the local control-plane snapshot even while transport is stopped."""
        with self._lock:
            return deepcopy(self._public_status)

    def _update_public_status(self):
        if self._public is not None:
            snapshot = self._public.status()
            with self._lock:
                self._public_status = deepcopy(snapshot)

    @staticmethod
    def _public_group_id(value):
        if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("Expected a lowercase public group hash")
        return value

    async def join_public_group(self, url: str, network_id: str | None = None) -> dict:
        descriptor = group_descriptor(self._default_network if network_id is None else network_id, url)
        return await self._call_public("join", descriptor["url"], descriptor["network_id"])

    async def leave_public_group(self, group_hash: str) -> dict:
        return await self._call_public("leave", self._public_group_id(group_hash))

    async def publish_public(self, group_hash: str, data) -> dict:
        return await self._call_public("publish", self._public_group_id(group_hash), data)

    async def read_public(self, group_hash: str, after: str | None = None, limit: int = 100) -> dict:
        group_hash = self._public_group_id(group_hash)
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError("Public message limit must be between 1 and 256")
        if after is not None and (type(after) is not str
                or re.fullmatch(r"[0-9a-f]{32}:(?:0|[1-9][0-9]{0,19})", after) is None):
            raise ValueError("Invalid public message cursor")
        return await self._call_public("read", group_hash, after, limit)

    async def _call_public(self, operation: str, *arguments):
        """Cancel the live Trio operation when its asyncio caller is cancelled."""
        service, token = self._public, self._token
        if (not self.config.enabled or not self.config.publicPubsubEnabled or service is None
                or token is None or not self._ready.is_set() or self._stop.is_set()
                or self._error is not None or self._thread is None or not self._thread.is_alive()):
            raise PublicPubsubUnavailable("Public pubsub is not running")
        state = {"scope": None, "cancelled": False}

        async def invoke():
            with trio.CancelScope() as scope:
                state["scope"] = scope
                if state["cancelled"]:
                    scope.cancel()
                await trio.lowlevel.checkpoint_if_cancelled()
                if service is not self._public or self._stop.is_set():
                    raise PublicPubsubUnavailable("Public pubsub runtime changed")
                try:
                    self._public_slots.acquire_nowait()
                except trio.WouldBlock:
                    raise PublicPubsubBusy("Public pubsub control is at capacity") from None
                try:
                    with trio.fail_after(self.config.requestTimeoutSeconds):
                        result = await getattr(service, operation)(*arguments)
                finally:
                    self._public_slots.release()
                    self._update_public_status()
            if scope.cancelled_caught:
                raise PublicPubsubUnavailable("Public pubsub request cancelled")
            return result

        def cancel():
            state["cancelled"] = True
            if state["scope"] is not None:
                state["scope"].cancel()

        try:
            return await asyncio.to_thread(partial(trio.from_thread.run, invoke, trio_token=token))
        except asyncio.CancelledError:
            try:
                token.run_sync_soon(cancel)
            except trio.RunFinishedError:
                pass
            raise
        except (trio.RunFinishedError, trio.Cancelled, trio.TooSlowError) as error:
            raise PublicPubsubUnavailable("Public pubsub request stopped or timed out") from error

    def public_record(self):
        with self._lock:
            packet = deepcopy(self._record)
        if packet is None or packet["body"]["expires_at"] <= int(time.time()):
            return None
        return packet

    def _amino_status(self, **values):
        with self._lock:
            self._amino.update(values)

    def _publication_status(self, **values):
        with self._lock:
            self._publication.update(values)

    async def find_operator(self, operator_hash: str, network: str = "syn") -> list[dict]:
        peer_record.provider_key(operator_hash)
        if type(network) is not str or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", network) is None:
            raise ValueError("Invalid discovery network")
        if (not self.config.enabled or not self.config.aminoEnabled or self._token is None
                or self._dht is None or self._thread is None or not self._thread.is_alive()):
            raise DiscoveryUnavailable("Amino discovery is not running")
        token = self._token
        # asyncio.to_thread cancellation alone leaves its Trio call running.
        # Keep this state on the Trio thread and explicitly cancel that scope,
        # including a request cancelled before the worker has entered Trio.
        state = {"scope": None, "cancelled": False}

        async def lookup():
            with trio.CancelScope() as scope:
                state["scope"] = scope
                if state["cancelled"]:
                    scope.cancel()
                result = await self._find_operator(operator_hash, network)
            if scope.cancelled_caught:
                raise DiscoveryUnavailable("Amino discovery request cancelled")
            return result

        def cancel():
            state["cancelled"] = True
            if state["scope"] is not None:
                state["scope"].cancel()

        call = partial(trio.from_thread.run, lookup, trio_token=token)
        try:
            return await asyncio.to_thread(call)
        except asyncio.CancelledError:
            try:
                token.run_sync_soon(cancel)
            except trio.RunFinishedError:
                pass
            raise
        except (trio.RunFinishedError, trio.Cancelled) as error:
            raise DiscoveryUnavailable("Amino discovery stopped") from error

    async def _find_operator(self, operator_hash, network):
        try:
            self._lookup_slots.acquire_nowait()
        except trio.WouldBlock:
            raise DiscoveryBusy("Amino discovery is at capacity") from None
        records = []
        try:
            with trio.move_on_after(self.config.lookupTimeoutSeconds) as scope:
                providers = await self._dht.find_providers(peer_record.provider_key(operator_hash), count=20)
                if not providers and self._dht.status()["lastLookupStatus"] == "unavailable":
                    raise DiscoveryUnavailable("No Amino servers answered the lookup")
                slots = trio.Semaphore(3)
                async def fetch(peer):
                    async with slots:
                        try:
                            peer = seed_dial_addresses(self._host, peer,
                                allow_private=self.config.allowPrivateAddresses)
                            packet = await fetch_peer_record(self._host, peer,
                                expected_operator=operator_hash, expected_network=network,
                                timeout=self.config.requestTimeoutSeconds)
                            allowed = dialable_tcp_addresses(
                                [multiaddr.Multiaddr(v) for v in packet["body"]["addresses"]],
                                peer_id=peer.peer_id, allow_private=self.config.allowPrivateAddresses)
                            # Return the signed packet intact only when every
                            # endpoint satisfies the caller's discovery policy.
                            if allowed and len(allowed) == len(packet["body"]["addresses"]):
                                records.append(packet)
                        except Exception:
                            logger.debug("Rejected or unreachable Operator provider candidate")
                async with trio.open_nursery() as nursery:
                    for candidate in providers:
                        nursery.start_soon(fetch, candidate)
            if scope.cancelled_caught and not records:
                raise DiscoveryUnavailable("Amino lookup timed out")
            unique = {record["body"]["peer_id"]: record for record in records
                      if record["body"]["expires_at"] > int(time.time())}
            return sorted(unique.values(), key=lambda p: (-p["body"]["issued_at"], p["body"]["peer_id"]))
        finally:
            self._lookup_slots.release()

    def _run(self) -> None:
        try:
            trio.run(self._serve)
        except BaseException as error:
            self._error = error
            self._amino_status(state="failed", lastError=type(error).__name__)
            logger.exception("libp2p node stopped with an error")
        finally:
            with self._lock:
                self._record = None
                self._public_status.update(running=False, groups=[])
            self._public = self._public_slots = None
            self._host = self._dht = self._token = self._startup_scope = None
            self._ready.set()

    def _public_addresses(self, host):
        candidates = list(host.get_addrs())
        if not self.config.announceAddresses and host.upnp is not None:
            external = host.upnp.get_external_ip()
            # Announce only successful mappings of actual IPv4 listeners.
            v4_ports = set()
            for addr in host.get_transport_addrs():
                try:
                    if addr.value_for_protocol("ip4") is not None:
                        v4_ports.add(int(addr.value_for_protocol("tcp")))
                except Exception:
                    continue
            if external:
                for port in sorted(self._mapped_ports & v4_ports):
                    try:
                        candidates.append(multiaddr.Multiaddr(f"/ip4/{external}/tcp/{port}"))
                    except ValueError:
                        continue
        return dialable_tcp_addresses(candidates, peer_id=host.get_id(),
                                     allow_private=self.config.allowPrivateAddresses)[:16]

    async def _serve(self) -> None:
        self._token = trio.lowlevel.current_trio_token()
        self._lookup_slots = trio.Semaphore(4)
        self._public_slots = trio.Semaphore(16)
        self._mapped_ports.clear()
        with trio.CancelScope() as scope:
            self._startup_scope = scope
            addresses = [multiaddr.Multiaddr(value) for value in self.config.listenAddresses]
            key_pair = create_new_key_pair()
            host = new_host(key_pair=key_pair, bootstrap=[],
                enable_upnp=self.config.upnpEnabled,
                announce_addrs=([multiaddr.Multiaddr(a) for a in self.config.announceAddresses]
                                if self.config.announceAddresses else None),
                sec_opt={NOISE_PROTOCOL: NoiseTransport(key_pair, noise_key_pair().private_key)},
                muxer_opt={YAMUX_PROTOCOL: Yamux})
            self._host = host
            if host.upnp is not None:
                original_mapping = host.upnp.add_port_mapping
                async def tracked_mapping(port, protocol="TCP"):
                    result = await original_mapping(port, protocol)
                    if result and protocol == "TCP":
                        self._mapped_ports.add(int(port))
                    return result
                host.upnp.add_port_mapping = tracked_mapping
            if self.config.aminoEnabled:
                self._dht = AminoDHTClient(host, self.config.bootstrapPeers,
                    timeout=self.config.requestTimeoutSeconds,
                    allow_private=self.config.allowPrivateAddresses)
            publish = self.operator is not None and self.config.publishOperatorRecord
            if publish:
                public = await trio.to_thread.run_sync(
                    lambda: self.operator.signer.public_key, abandon_on_cancel=True)
                if hashlib.sha256(public).hexdigest() != self.operator.expected_hash:
                    raise ValueError("P2P Operator signer differs from installed base identity")
                service = PeerRecordService(host, public, self.public_record,
                                           timeout=self.config.requestTimeoutSeconds)
                service.register()
                self._publication_status(providerKey=peer_record.provider_key(self.operator.expected_hash).hex(),
                    cid=peer_record.provider_cid(self.operator.expected_hash))
            async with host.run(listen_addrs=addresses):
                if self.config.mdnsEnabled:
                    host.mDNS = MDNSDiscovery(host.get_network(),
                        port=_advertisedTcpPort(host.get_transport_addrs()))
                    host.mDNS.start()
                with self._lock:
                    self._peerId = str(host.get_id())
                    self._addresses = tuple(str(value) for value in host.get_transport_addrs())
                    self._upnpExternalIp = host.upnp.get_external_ip() if host.upnp is not None else None
                async with trio.open_nursery() as nursery:
                    if self.config.publicPubsubEnabled and not self._stop.is_set():
                        self._public = PublicPubsubService(host, self._dht, self.config,
                            lambda: self._public_addresses(host), default_network=self._default_network)
                        await nursery.start(self._public.run)
                        self._update_public_status()
                    self._ready.set()
                    if self._dht is not None:
                        nursery.start_soon(self._bootstrap_loop)
                    else:
                        self._amino_status(state="disabled")
                    if publish:
                        nursery.start_soon(self._publication_loop, host, key_pair.public_key.to_bytes())
                    while not self._stop.is_set():
                        self._update_public_status()
                        discovered = tuple(sorted(str(value) for value in host.get_peerstore().valid_peer_ids()
                                                  if value != host.get_id()))
                        with self._lock:
                            self._discoveredPeers = discovered
                            if self._dht is not None:
                                self._amino.update(self._dht.status())
                        await trio.sleep(0.1)
                    nursery.cancel_scope.cancel()
            self._amino_status(state="stopped")

    async def _bootstrap_loop(self):
        retry = 5.0
        while True:
            self._amino_status(state="bootstrapping")
            try:
                with trio.fail_after(self.config.lookupTimeoutSeconds):
                    peers = await self._dht.bootstrap()
                if not peers:
                    raise DiscoveryUnavailable("No responding Amino bootstrap peers")
                self._amino_status(state="ready", routingPeers=peers,
                                   lastBootstrapAt=int(time.time()), lastError=None)
                retry = 5.0
                delay = self.config.bootstrapRefreshSeconds
            except Exception as error:
                self._amino_status(state="unavailable", lastError=type(error).__name__)
                delay, retry = retry, min(retry * 2, 300)
            await trio.sleep(delay)

    async def _publication_loop(self, host, transport_public):
        current_addresses, next_announcement, retry = None, 0.0, 5.0
        signer = peer_record.OperatorRecordSigner(self.operator)
        while True:
            addresses = sorted(str(addr) for addr in self._public_addresses(host))
            now = int(time.time())
            packet = self.public_record()
            if not addresses:
                with self._lock:
                    self._record = None
                current_addresses = None
                self._publication_status(state="waiting-addresses", addresses=[], expiresAt=None)
            else:
                changed = addresses != current_addresses
                if changed or packet is None or packet["body"]["expires_at"] - now <= 1800:
                    # First Windows approval may wait for native UI. Local host
                    # readiness and the Trio event loop must remain responsive.
                    if changed:
                        with self._lock:
                            self._record = None
                    self._publication_status(state="signing", addresses=addresses)
                    packet = await trio.to_thread.run_sync(
                        partial(signer.issue, transport_public, addresses), abandon_on_cancel=True)
                    if self._stop.is_set():
                        return
                    with self._lock:
                        self._record = packet
                    self._publication_status(state=("announced" if not changed and self._publication["lastAnnouncedAt"]
                                                    else "signed"),
                        expiresAt=packet["body"]["expires_at"], lastError=None)
                    if changed:
                        next_announcement = 0.0
                    current_addresses = addresses
                if (self._dht is not None and self._dht.status()["routingPeers"] > 0
                        and trio.current_time() >= next_announcement):
                    try:
                        with trio.fail_after(self.config.lookupTimeoutSeconds):
                            count = await self._dht.provide(peer_record.provider_key(self.operator.expected_hash),
                                [multiaddr.Multiaddr(a) for a in addresses])
                        if not count:
                            raise DiscoveryUnavailable("No Amino provider announcement could be sent")
                        # A successful write is not a storage acknowledgement.
                        self._publication_status(state="announced", announcedTo=count,
                            lastAnnouncedAt=int(time.time()), lastError=None)
                        retry = 5.0
                        next_announcement = trio.current_time() + self.config.providerRepublishSeconds
                    except Exception as error:
                        self._publication_status(state="unavailable", lastError=type(error).__name__)
                        next_announcement = trio.current_time() + retry
                        retry = min(retry * 2, 300)
            await trio.sleep(1)


def _advertisedTcpPort(addresses: list[multiaddr.Multiaddr]) -> int:
    for address in addresses:
        try:
            if address.value_for_protocol("ip4") is not None:
                return int(address.value_for_protocol("tcp"))
        except Exception:
            continue
    raise RuntimeError("mDNS requires at least one IPv4 TCP listen address")
