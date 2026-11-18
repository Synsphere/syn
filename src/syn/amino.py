"""Bounded Amino DHT client on py-libp2p's authenticated stream transport.

Only the public FIND_NODE / GET_PROVIDERS / ADD_PROVIDER RPCs are used. This
module never registers a Kademlia stream handler: a Syn node is a DHT client,
including when it provides its Operator's public-key block. We intentionally
do not use py-libp2p 0.7's text-key provider wrappers, unbounded response readers,
or ADD_PROVIDER acknowledgement assumption. Kubo sends no acknowledgement.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
import hashlib
import ipaddress
import math

import dns.asyncresolver
from multiaddr import Multiaddr
import trio

from libp2p.kad_dht.pb.kademlia_pb2 import Message
from libp2p.peer.id import ID
from libp2p.peer.peerinfo import PeerInfo

from .p2p_defaults import AMINO_PROTOCOL, IPFS_BOOTSTRAP_PEERS

K = 20
ALPHA = 10
MAX_FRAME_BYTES = 128 * 1024
MAX_RESPONSE_PEERS = 64
MAX_PROVIDERS = 64
MAX_ADDRESSES = 16
MAX_ADDRESS_BYTES = 1024
MAX_CANDIDATES = 256
MAX_KNOWN_PEERS = 512
MAX_QUERIES = 64
MAX_BOOTSTRAP_PEERS = 32
MAX_DNS_DEPTH = 4
MAX_DNS_RESULTS = 32
MAX_DNS_QUERIES = 16
ADDRESS_TTL_SECONDS = 600


def _varint(value: int) -> bytes:
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _take_varint(raw: bytes, start: int = 0) -> tuple[int, int]:
    value = 0
    for offset in range(5):
        index = start + offset
        if index >= len(raw):
            raise ValueError("Truncated unsigned varint")
        byte = raw[index]
        value |= (byte & 127) << (7 * offset)
        if byte < 128:
            if _varint(value) != raw[start:index + 1]:
                raise ValueError("Noncanonical unsigned varint")
            return value, index + 1
    raise ValueError("Oversized unsigned varint")


def _peer_id(raw: bytes) -> ID:
    if not isinstance(raw, bytes) or not 3 <= len(raw) <= 64:
        raise ValueError("Invalid peer identifier size")
    codec, pos = _take_varint(raw)
    size, pos = _take_varint(raw, pos)
    if len(raw) != pos + size or not (
        (codec == 0x12 and size == 32) or (codec == 0 and 0 < size <= 42)
    ):
        raise ValueError("Invalid libp2p peer multihash")
    return ID(raw)


def _provider_key(key: bytes) -> None:
    # Syn provides a raw Ed25519 public-key block, identified by SHA2-256.
    if not isinstance(key, bytes) or len(key) != 34 or key[:2] != b"\x12\x20":
        raise ValueError("Amino provider key must be a binary SHA2-256 multihash")


def dialable_tcp_addresses(
    addresses: Iterable[Multiaddr], *, peer_id: ID | str | None = None,
    allow_private: bool = False,
) -> list[Multiaddr]:
    """Return bounded, concrete TCP addresses with any own Peer ID removed.

    Public discovery accepts globally routable IPs only. ``allow_private`` is
    an explicit isolated-lab option; wildcard and multicast remain invalid.
    DNS bootstrap addresses are resolved separately before reaching this filter.
    Unsupported transport stacks and mismatched terminal Peer IDs are ignored.
    """
    result: list[Multiaddr] = []
    seen: set[bytes] = set()
    expected = str(peer_id) if peer_id is not None else None
    for index, value in enumerate(addresses):
        if index >= MAX_DNS_RESULTS:
            break
        try:
            address = value if isinstance(value, Multiaddr) else Multiaddr(value)
            if len(address.to_bytes()) > MAX_ADDRESS_BYTES:
                continue
            protocols = [p.name for p in address.protocols()]
            if protocols[-1:] == ["p2p"]:
                suffix = address.value_for_protocol("p2p")
                if expected is not None and suffix != expected:
                    continue
                _peer_id(ID.from_base58(suffix).to_bytes())
                protocols = protocols[:-1]
            if protocols not in (["ip4", "tcp"], ["ip6", "tcp"]):
                continue
            ip = ipaddress.ip_address(address.value_for_protocol(protocols[0]))
            port = int(address.value_for_protocol("tcp"))
            if not 1 <= port <= 65535 or ip.is_unspecified or ip.is_multicast:
                continue
            if not allow_private and not ip.is_global:
                continue
            normalized = Multiaddr(f"/ip{ip.version}/{ip.compressed}/tcp/{port}")
            encoded = normalized.to_bytes()
            if encoded not in seen:
                result.append(normalized)
                seen.add(encoded)
            if len(result) == MAX_ADDRESSES:
                break
        except (ValueError, TypeError, KeyError):
            continue
    return result


def seed_dial_addresses(host, peer_info: PeerInfo, *, allow_private=False) -> PeerInfo:
    """Replace a peer's dial addresses with validated transport addresses.

    Identify or local discovery may previously have populated this shared
    peerstore. Appending public DHT addresses would leave those older routes
    eligible for a new dial, so reseed the address list immediately before
    opening an authenticated stream. Existing authenticated connections to the
    same Peer ID can still be reused by the host.
    """
    peer_id = _peer_id(peer_info.peer_id.to_bytes())
    addresses = dialable_tcp_addresses(
        peer_info.addrs, peer_id=peer_id, allow_private=allow_private,
    )
    if not addresses:
        raise ValueError("Peer has no permitted concrete TCP addresses")
    store = host.get_peerstore()
    store.clear_addrs(peer_id)
    store.add_addrs(peer_id, addresses, ADDRESS_TTL_SECONDS)
    return PeerInfo(peer_id, addresses)


async def _read_frame(stream) -> bytes:
    prefix = bytearray()
    for _ in range(5):
        byte = await stream.read(1)
        if len(byte) != 1:
            raise ValueError("Truncated Amino frame prefix")
        prefix.extend(byte)
        if byte[0] < 128:
            break
    else:
        raise ValueError("Oversized Amino frame prefix")
    size, _ = _take_varint(bytes(prefix))
    if not 0 < size <= MAX_FRAME_BYTES:
        raise ValueError("Amino response exceeds frame bound")
    result = bytearray()
    while len(result) < size:
        chunk = await stream.read(size - len(result))
        if not chunk or len(chunk) > size - len(result):
            raise ValueError("Truncated Amino response")
        result.extend(chunk)
    return bytes(result)


class AminoDHTClient:
    """Client-only, bounded provider routing; all operations run in one Trio loop.

    The host owns encrypted transport authentication and its resource limits.
    A provider result is an untrusted discovery candidate, never an assertion
    of Syn Operator identity or permission. Verify the Syn peer binding after
    connecting to that candidate.
    """

    def __init__(self, host, bootstrap_peers, *, timeout=10.0, allow_private=False):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("Amino request timeout must be finite and in (0, 120]")
        if type(allow_private) is not bool:
            raise ValueError("Amino private-address override must be boolean")
        if not isinstance(bootstrap_peers, (tuple, list)) or len(bootstrap_peers) > MAX_BOOTSTRAP_PEERS:
            raise ValueError("Too many Amino bootstrap peers")
        if any(not isinstance(p, str) or not p or len(p) > MAX_ADDRESS_BYTES for p in bootstrap_peers):
            raise ValueError("Invalid Amino bootstrap address")
        self.host = host
        self.bootstrap_peers = tuple(bootstrap_peers)
        self.timeout = float(timeout)
        self.allow_private = allow_private
        self._known: OrderedDict[bytes, PeerInfo] = OrderedDict()
        self._servers: set[bytes] = set()
        self._operation = trio.Lock()
        self._bootstrap_attempts = 0
        self._bootstrap_status = "pending"
        self._bootstrap_count = 0
        self._announcement_status = "idle"
        self._announcements = 0
        self._last_announcements = 0
        self._query_failures = 0
        self._lookup_status = "idle"
        self._lookup_peers = 0
        self._lookup_providers = 0

    def status(self) -> dict:
        return {
            "protocol": str(AMINO_PROTOCOL), "mode": "client",
            "routingPeers": len(self._servers),
            "bootstrapStatus": self._bootstrap_status,
            "bootstrapAttempts": self._bootstrap_attempts,
            "lastBootstrapPeers": self._bootstrap_count,
            "announcementStatus": self._announcement_status,
            "announcementsSent": self._announcements,
            "lastAnnouncementsSent": self._last_announcements,
            "queryFailures": self._query_failures,
            "lastLookupStatus": self._lookup_status,
            "lastLookupPeers": self._lookup_peers,
            "lastLookupProviders": self._lookup_providers,
        }

    def _remember(self, info: PeerInfo, *, server=False) -> None:
        raw = info.peer_id.to_bytes()
        if raw == self.host.get_id().to_bytes():
            return
        self._known[raw] = info
        self._known.move_to_end(raw)
        if server:
            self._servers.add(raw)
        while len(self._known) > MAX_KNOWN_PEERS:
            expired, _ = self._known.popitem(last=False)
            self._servers.discard(expired)

    async def _dns_records(self, hostname: str, kind: str):
        answer = await dns.asyncresolver.resolve(hostname, kind, lifetime=self.timeout)
        return list(answer)[:MAX_DNS_RESULTS]

    async def _resolve_bootstrap(self, text: str) -> PeerInfo | None:
        address = Multiaddr(text)
        protocols = [p.name for p in address.protocols()]
        if protocols[-1:] != ["p2p"] or protocols.count("p2p") != 1:
            raise ValueError("Bootstrap address must end in a pinned Peer ID")
        expected_text = address.value_for_protocol("p2p")
        expected = _peer_id(ID.from_base58(expected_text).to_bytes())
        found: list[Multiaddr] = []
        visited: set[str] = set()
        queries = 0

        async def walk(current: Multiaddr, depth: int) -> None:
            nonlocal queries
            if depth > MAX_DNS_DEPTH or len(found) >= MAX_ADDRESSES or str(current) in visited:
                return
            visited.add(str(current))
            names = [p.name for p in current.protocols()]
            if names[-1:] != ["p2p"] or current.value_for_protocol("p2p") != expected_text:
                return
            concrete = dialable_tcp_addresses([current], peer_id=expected, allow_private=self.allow_private)
            if concrete:
                found.extend(concrete)
                return
            if not names or names[0] not in {"dnsaddr", "dns", "dns4", "dns6"}:
                return
            hostname = current.value_for_protocol(names[0])
            if not hostname or len(hostname) > 253 or not hostname.isascii() or any(c.isspace() for c in hostname):
                return
            if names == ["dnsaddr", "p2p"]:
                if queries >= MAX_DNS_QUERIES:
                    return
                queries += 1
                answers = await self._dns_records("_dnsaddr." + hostname, "TXT")
                for answer in answers[:MAX_DNS_RESULTS]:
                    value = b"".join(answer.strings)
                    if not value.startswith(b"dnsaddr=") or len(value) > MAX_ADDRESS_BYTES:
                        continue
                    try:
                        child = Multiaddr(value[8:].decode("ascii"))
                    except (ValueError, UnicodeError):
                        continue
                    await walk(child, depth + 1)
            elif names in (["dns", "tcp", "p2p"], ["dns4", "tcp", "p2p"], ["dns6", "tcp", "p2p"]):
                port = int(current.value_for_protocol("tcp"))
                if not 1 <= port <= 65535:
                    return
                kinds = ("A", "AAAA") if names[0] == "dns" else (("A",) if names[0] == "dns4" else ("AAAA",))
                for kind in kinds:
                    if queries >= MAX_DNS_QUERIES:
                        return
                    queries += 1
                    try:
                        answers = await self._dns_records(hostname, kind)
                    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                        continue
                    for answer in answers[:MAX_DNS_RESULTS]:
                        family = "ip4" if kind == "A" else "ip6"
                        child = Multiaddr(f"/{family}/{answer.address}/tcp/{port}/p2p/{expected_text}")
                        await walk(child, depth + 1)

        with trio.fail_after(self.timeout):
            await walk(address, 0)
        addrs = dialable_tcp_addresses(found, peer_id=expected, allow_private=self.allow_private)
        return PeerInfo(expected, addrs) if addrs else None

    def _decode_peers(self, entries) -> list[PeerInfo]:
        result = []
        for entry in entries:
            try:
                peer = _peer_id(bytes(entry.id))
                if peer == self.host.get_id():
                    continue
                addresses = [Multiaddr(raw) for raw in entry.addrs[:MAX_ADDRESSES]
                             if len(raw) <= MAX_ADDRESS_BYTES]
                addresses = dialable_tcp_addresses(addresses, peer_id=peer, allow_private=self.allow_private)
                if addresses:
                    result.append(PeerInfo(peer, addresses))
            except (ValueError, TypeError, KeyError):
                continue
        return result

    async def _request(self, info: PeerInfo, message: Message, *, response=True) -> Message | None:
        stream = None
        completed = False
        try:
            with trio.fail_after(self.timeout):
                info = seed_dial_addresses(self.host, info, allow_private=self.allow_private)
                stream = await self.host.new_stream(info.peer_id, [AMINO_PROTOCOL])
                encoded = message.SerializeToString()
                if len(encoded) > MAX_FRAME_BYTES:
                    raise ValueError("Amino request exceeds frame bound")
                await stream.write(_varint(len(encoded)) + encoded)
                if not response:
                    # Kubo's ADD_PROVIDER is fire-and-forget. This counts a sent
                    # announcement, never an acknowledgement of remote storage.
                    completed = True
                    return None
                raw = await _read_frame(stream)
                packet = Message()
                packet.ParseFromString(raw)
                if packet.type != message.type or (packet.key and packet.key != message.key):
                    raise ValueError("Amino response does not match its request")
                if len(packet.closerPeers) > MAX_RESPONSE_PEERS or len(packet.providerPeers) > MAX_RESPONSE_PEERS:
                    raise ValueError("Amino response contains too many peers")
                completed = True
                self._remember(info, server=True)
                return packet
        finally:
            if stream is not None:
                # Cancellation must propagate, but still close/reset its stream.
                with trio.CancelScope(shield=True):
                    with trio.move_on_after(min(self.timeout, 1.0)):
                        try:
                            if completed:
                                await stream.close()
                            else:
                                await stream.reset()
                        except Exception:
                            pass

    async def _query(self, info: PeerInfo, kind: int, key: bytes) -> Message | None:
        try:
            return await self._request(info, Message(type=kind, key=key))
        except Exception:
            self._query_failures += 1
            self._servers.discard(info.peer_id.to_bytes())
            return None

    @staticmethod
    def _distance(info: PeerInfo, target: int) -> int:
        return int.from_bytes(hashlib.sha256(info.peer_id.to_bytes()).digest(), "big") ^ target

    async def _lookup(self, key: bytes, kind: int, count=K):
        target = int.from_bytes(hashlib.sha256(key).digest(), "big")
        seeds = sorted(self._known.values(), key=lambda p: self._distance(p, target))[:MAX_CANDIDATES]
        candidates = {p.peer_id.to_bytes(): p for p in seeds}
        queried: set[bytes] = set()
        failed: set[bytes] = set()
        successful: dict[bytes, PeerInfo] = {}
        providers: dict[bytes, PeerInfo] = {}
        while len(queried) < MAX_QUERIES:
            nearest = sorted((info for raw, info in candidates.items() if raw not in failed),
                             key=lambda info: self._distance(info, target))[:K]
            pending = [info for info in nearest if info.peer_id.to_bytes() not in queried]
            batch = pending[:min(ALPHA, MAX_QUERIES - len(queried))]
            if not batch:
                break
            responses: list[tuple[PeerInfo, Message | None]] = []

            async def query(info):
                queried.add(info.peer_id.to_bytes())
                responses.append((info, await self._query(info, kind, key)))

            async with trio.open_nursery() as nursery:
                for info in batch:
                    nursery.start_soon(query, info)
            for info, response in responses:
                raw = info.peer_id.to_bytes()
                if response is None:
                    failed.add(raw)
                    continue
                successful[raw] = info
                for closer in self._decode_peers(response.closerPeers):
                    candidates[closer.peer_id.to_bytes()] = closer
                    self._remember(closer)
                if kind == Message.GET_PROVIDERS:
                    for provider in self._decode_peers(response.providerPeers):
                        if len(providers) >= MAX_PROVIDERS:
                            break
                        providers[provider.peer_id.to_bytes()] = provider
                if len(candidates) > MAX_CANDIDATES:
                    keep = sorted(candidates.values(), key=lambda p: self._distance(p, target))[:MAX_CANDIDATES]
                    candidates = {p.peer_id.to_bytes(): p for p in keep}
            if kind == Message.GET_PROVIDERS and len(providers) >= count:
                break
        closest = sorted(successful.values(), key=lambda p: self._distance(p, target))[:K]
        return closest, list(providers.values())[:count]

    async def bootstrap(self) -> int:
        """Resolve pins and query the DHT; return responsive server count.

        Unavailable bootstrappers are ordinary retryable discovery failures.
        Existing contacts can be used if DNS or one seed is temporarily down.
        """
        async with self._operation:
            self._bootstrap_attempts += 1
            self._bootstrap_status = "connecting"
            successful: set[bytes] = set()

            async def connect(value):
                try:
                    info = await self._resolve_bootstrap(value)
                    if info is None or info.peer_id == self.host.get_id():
                        return
                    self._remember(info)
                    response = await self._query(info, Message.FIND_NODE, self.host.get_id().to_bytes())
                    if response is not None:
                        successful.add(info.peer_id.to_bytes())
                        for closer in self._decode_peers(response.closerPeers):
                            self._remember(closer)
                except Exception:
                    self._query_failures += 1

            # At most ALPHA DNS/dial tasks at once, including explicit overrides.
            for start in range(0, len(self.bootstrap_peers), ALPHA):
                async with trio.open_nursery() as nursery:
                    for value in self.bootstrap_peers[start:start + ALPHA]:
                        nursery.start_soon(connect, value)
            closest, _ = await self._lookup(self.host.get_id().to_bytes(), Message.FIND_NODE)
            successful.update(p.peer_id.to_bytes() for p in closest)
            self._bootstrap_count = len(successful)
            self._bootstrap_status = "ready" if successful else "unavailable"
            return self._bootstrap_count

    async def provide(self, key: bytes, addresses: list[Multiaddr]) -> int:
        """Send own-provider advertisements; return writes, not storage ACKs."""
        _provider_key(key)
        addresses = dialable_tcp_addresses(addresses, peer_id=self.host.get_id(), allow_private=self.allow_private)
        async with self._operation:
            self._last_announcements = 0
            if not addresses:
                self._announcement_status = "no-public-addresses"
                return 0
            closest, _ = await self._lookup(key, Message.FIND_NODE)
            message = Message(type=Message.ADD_PROVIDER, key=key)
            provider = message.providerPeers.add()
            provider.id = self.host.get_id().to_bytes()
            provider.addrs.extend(address.to_bytes() for address in addresses)

            async def announce(info):
                try:
                    await self._request(info, message, response=False)
                    self._last_announcements += 1
                except Exception:
                    self._query_failures += 1
                    self._servers.discard(info.peer_id.to_bytes())

            for start in range(0, len(closest), ALPHA):
                async with trio.open_nursery() as nursery:
                    for info in closest[start:start + ALPHA]:
                        nursery.start_soon(announce, info)
            self._announcements += self._last_announcements
            self._announcement_status = "sent" if self._last_announcements else "unavailable"
            return self._last_announcements

    async def find_providers(self, key: bytes, count=20) -> list[PeerInfo]:
        _provider_key(key)
        if type(count) is not int or not 1 <= count <= K:
            raise ValueError("Provider result count must be between 1 and 20")
        async with self._operation:
            self._lookup_status = "querying"
            self._lookup_peers = self._lookup_providers = 0
            closest, providers = await self._lookup(key, Message.GET_PROVIDERS, count)
            self._lookup_peers = len(closest)
            self._lookup_providers = len(providers)
            self._lookup_status = "found" if providers else ("empty" if closest else "unavailable")
            return providers
