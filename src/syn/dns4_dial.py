"""Bounded DNS4 resolution for the managed Syn py-libp2p Swarm.

Upstream 0.8.1 has a DNSResolver object but Swarm.dial_peer filters raw
/dns4/ addresses by the registered transport before using it. Translate
validated DNS4 peerstore entries to concrete IPv4 transport addresses before
that dial, preserving the caller's pinned PeerID and connection gate.
"""
from __future__ import annotations

import hashlib
from itertools import islice
import ipaddress
import re
from collections import OrderedDict

MAX_PEER_ADDRESSES = 32
MAX_DNS_RESULTS = 4
MAX_NAME_LENGTH = 253
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_CERTHASH = re.compile(r"[A-Za-z0-9]{16,256}\Z")


def _parse_dns4(value: str, *, peer_id: str) -> tuple[str, str] | None:
    """Validate DNS4 grammar, transport suffix, and any pinned PeerID."""
    if not value.startswith("/dns4/") or len(value) > 512:
        return None
    parts = value.split("/")
    if len(parts) < 5 or parts[:2] != ["", "dns4"]:
        return None
    name = parts[2].lower().rstrip(".")
    if not 0 < len(name) <= MAX_NAME_LENGTH or not all(
            _DNS_LABEL.fullmatch(label) for label in name.split(".")):
        return None
    suffix = parts[3:]
    if len(suffix) >= 2 and suffix[-2] == "p2p":
        if suffix[-1] != peer_id:
            return None
        suffix = suffix[:-2]
    if len(suffix) < 2:
        return None
    try:
        port = int(suffix[1])
    except ValueError:
        return None
    if str(port) != suffix[1] or not 1 <= port <= 65535:
        return None
    tail = suffix[2:]
    if suffix[0] == "tcp":
        if tail not in ([], ["ws"], ["wss"]):
            return None
    elif suffix[0] == "udp":
        if tail == ["quic-v1"]:
            pass
        elif (len(tail) == 3 and tail[0] == "webrtc-direct" and
                tail[1] == "certhash" and _CERTHASH.fullmatch(tail[2])):
            pass
        elif (len(tail) >= 4 and len(tail) % 2 == 0 and
              tail[:2] == ["quic-v1", "webtransport"] and
              all(tail[i] == "certhash" and _CERTHASH.fullmatch(tail[i+1])
                  for i in range(2, len(tail), 2))):
            pass
        else:
            return None
    else:
        return None
    return name, "/" + "/".join(suffix)


async def resolve_dns4(value: str, *, peer_id: str, resolve_a, allow_private=False):
    """Resolve at most four A records, rejecting private/rebinding destinations.

    The injected resolver is awaited once; the production implementation uses
    dnspython with a bounded lifetime. No untrusted DNS response can alter the
    remote PeerID or the already-selected transport stack.
    """
    parsed = _parse_dns4(value, peer_id=peer_id)
    if parsed is None:
        return []
    hostname, suffix = parsed
    raw_addresses = await resolve_a(hostname)
    output = []
    for value in islice(raw_addresses, MAX_DNS_RESULTS):
        try:
            ip = ipaddress.IPv4Address(str(value))
        except ipaddress.AddressValueError:
            continue
        if (ip.is_unspecified or ip.is_multicast or ip.is_link_local or
                (not allow_private and not ip.is_global)):
            continue
        address = f"/ip4/{ip.compressed}{suffix}"
        if address not in output:
            output.append(address)
    return output


def install_dns4_dialer(host, *, timeout=3.0, allow_private=False):
    """Hook only this managed host's Swarm; leave py-libp2p globals unchanged.

    Covers explicit host.connect and upstream background auto-dial attempts.
    DNS failures do not change authenticated PeerID checks or the connection
    gate, and never result in a public-to-private-address downgrade.
    """
    import dns.asyncresolver
    from multiaddr import Multiaddr
    import trio

    swarm = host.get_network()
    store = host.get_peerstore()
    original = swarm.dial_peer
    gate = trio.Semaphore(4)
    # Fixed lock stripes keep parallel calls for one PeerID from racing the
    # peerstore replacement before the first DNS4 lookup completes.
    locks = tuple(trio.Lock() for _ in range(64))
    last_attempt = OrderedDict()  # bounded peer id -> next eligible time
    original_dns4 = OrderedDict()  # immutable names survive concrete-IP TTL expiry

    async def prepare(peer_id):
        key = peer_id.to_bytes()
        now = trio.current_time()
        if last_attempt.get(key, 0) > now:
            return
        last_attempt[key] = now + 30
        last_attempt.move_to_end(key)
        while len(last_attempt) > 512:
            last_attempt.popitem(last=False)
        try:
            addresses = list(store.addrs(peer_id))[:MAX_PEER_ADDRESSES]
        except Exception:
            addresses = []
        sources = [str(addr) for addr in addresses if str(addr).startswith("/dns4/")]
        if sources:
            original_dns4[key] = tuple(sources[:MAX_PEER_ADDRESSES])
            original_dns4.move_to_end(key)
            while len(original_dns4) > 512:
                original_dns4.popitem(last=False)
        else:
            sources = original_dns4.get(key, ())
        if not sources:
            return

        async def lookup(hostname):
            try:
                answer = await dns.asyncresolver.resolve(hostname, "A", lifetime=timeout)
            except Exception:
                return []
            return [entry.address for entry in answer][:MAX_DNS_RESULTS]

        resolved = [addr for addr in addresses if not str(addr).startswith("/dns4/")]
        for text in sources:
            for candidate in await resolve_dns4(
                    text, peer_id=str(peer_id), resolve_a=lookup, allow_private=allow_private):
                try:
                    resolved.append(Multiaddr(candidate))
                except ValueError:
                    continue
        # Only replace the peerstore if some concrete dial address remains.
        # Otherwise keep the original DNS4 entries for a later retry, while
        # reporting an ordinary unreachable peer rather than a false success.
        if resolved:
            # Another discovery job may have added fresh addresses while DNS
            # yielded. Preserve those concrete routes before reseeding.
            try:
                resolved.extend(addr for addr in store.addrs(peer_id)
                                if not str(addr).startswith("/dns4/"))
            except Exception:
                pass
            unique = list(dict.fromkeys(str(addr) for addr in resolved))[:MAX_PEER_ADDRESSES]
            store.clear_addrs(peer_id)
            store.add_addrs(peer_id, [Multiaddr(addr) for addr in unique], 60)

    async def dial_peer(peer_id):
        digest = hashlib.sha256(peer_id.to_bytes()).digest()
        lock = locks[int.from_bytes(digest[:2], "big") % len(locks)]
        async with gate:
            async with lock:
                # A timeout bounds total DNS latency across a peer's addresses.
                with trio.move_on_after(max(1.0, min(10.0, timeout * 2))):
                    await prepare(peer_id)
        return await original(peer_id)

    swarm.dial_peer = dial_peer
