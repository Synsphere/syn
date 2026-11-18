"""Opt-in, HMAC-authenticated IPv4 multicast reflector between WSL and LAN.

mDNS is enabled only with --mdns. Additional multicast groups require
--group GROUP:PORT on BOTH sides. This is a bounded UDP application relay,
not an Ethernet bridge: source IP and hop semantics cannot be preserved, and
some Bonjour/SSDP clients require them. No broadcast or WAN forwarding.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import selectors
import socket
import struct
import sys
import threading
import time

from syn_wsl_bridge_common import BridgeError, secret_file, valid_ipv4, valid_port

LOG = logging.getLogger("syn.wsl.multicast")
MDNS = ("224.0.0.251", 5353)
SSDP = ("239.255.255.250", 1900)
MAGIC = b"SWMC1"
HEADER = struct.Struct("!5sI4sHH8s")
MAX_PAYLOAD = 1400
MAX_CHANNELS = 8
MAX_RATE = 120
TIMESTAMP_SKEW = 30
HASH_TTL = 1.0


def channel(value: str) -> tuple[str, int]:
    if type(value) is not str or ":" not in value:
        raise BridgeError("Expected an approved IPv4 multicast GROUP:PORT")
    group, port = value.rsplit(":", 1)
    try:
        parsed = ipaddress.IPv4Address(group)
        integer = int(port)
        if not parsed.is_multicast or not 1024 <= integer <= 65535 or str(integer) != port:
            raise ValueError
    except (ValueError, TypeError, ipaddress.AddressValueError):
        raise BridgeError("Expected an approved IPv4 multicast GROUP:PORT") from None
    if group in ("224.0.0.1", "224.0.0.2", "224.0.0.22"):
        raise BridgeError("Infrastructure multicast groups are not approved for mirroring")
    return str(parsed), integer


def channels(*, mdns: bool = False, groups=()) -> tuple[tuple[str, int], ...]:
    found = ([MDNS] if mdns else []) + [channel(group) for group in groups]
    if len(found) > MAX_CHANNELS or len(set(found)) != len(found):
        raise BridgeError("Duplicate or excessive multicast channels")
    return tuple(found)


def _derived_key(secret: bytes) -> bytes:
    return hmac.new(secret, b"syn.wsl.multicast.v1\0", hashlib.sha256).digest()


def seal(secret: bytes, group: str, port: int, payload: bytes, *,
         timestamp: int | None = None, nonce: bytes | None = None) -> bytes:
    if (type(payload) is not bytes or not 0 < len(payload) <= MAX_PAYLOAD):
        raise BridgeError("Invalid multicast datagram size")
    group, port = channel(f"{group}:{port}")
    tick = int(time.time()) if timestamp is None else timestamp
    nonce = secrets.token_bytes(8) if nonce is None else nonce
    if type(nonce) is not bytes or len(nonce) != 8:
        raise BridgeError("Invalid multicast nonce")
    frame = HEADER.pack(MAGIC, tick, socket.inet_aton(group), port, len(payload), nonce) + payload
    return frame + hmac.new(_derived_key(secret), frame, hashlib.sha256).digest()[:16]


class FrameVerifier:
    def __init__(self, secret: bytes, allowed):
        self.key = _derived_key(secret)
        self.allowed = frozenset(allowed)
        self._seen: OrderedDict[bytes, int] = OrderedDict()

    def open(self, frame: bytes, *, now: int | None = None) -> tuple[tuple[str, int], bytes]:
        now = int(time.time()) if now is None else now
        if len(frame) < HEADER.size + 16 or len(frame) > HEADER.size + MAX_PAYLOAD + 16:
            raise BridgeError("Invalid multicast frame length")
        packet, mac = frame[:-16], frame[-16:]
        if not hmac.compare_digest(hmac.new(self.key, packet, hashlib.sha256).digest()[:16], mac):
            raise BridgeError("Multicast frame authentication failed")
        magic, tick, raw_group, port, size, nonce = HEADER.unpack_from(packet)
        selected = (socket.inet_ntoa(raw_group), port)
        if (magic != MAGIC or abs(tick - now) > TIMESTAMP_SKEW or
                selected not in self.allowed or size != len(packet) - HEADER.size or not 0 < size <= MAX_PAYLOAD):
            raise BridgeError("Unapproved or stale multicast frame")
        for value, previous in list(self._seen.items()):
            if previous < now - TIMESTAMP_SKEW: self._seen.pop(value, None)
        if nonce in self._seen:
            raise BridgeError("Multicast frame replay")
        self._seen[nonce] = now
        while len(self._seen) > 2048: self._seen.popitem(last=False)
        return selected, packet[HEADER.size:]


class _RateLimit:
    def __init__(self, max_per_second=MAX_RATE):
        self.budget, self.maximum = float(max_per_second), float(max_per_second)
        self.tick = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self.budget = min(self.maximum, self.budget + (now - self.tick) * self.maximum)
        self.tick = now
        if self.budget < 1:
            return False
        self.budget -= 1
        return True


class MulticastRelay:
    """One local multicast interface, one authenticated pinned tunnel peer."""

    def __init__(self, *, interface: str, tunnel_bind: str, tunnel_port: int,
                 peer_ip: str, peer_port: int, secret: bytes, groups):
        self.interface = valid_ipv4(interface)
        self.tunnel_bind, self.tunnel_port = valid_ipv4(tunnel_bind), valid_port(tunnel_port)
        self.peer = (valid_ipv4(peer_ip), valid_port(peer_port))
        self.groups = tuple(groups)
        if not self.groups or len(self.groups) > MAX_CHANNELS or len(set(self.groups)) != len(self.groups):
            raise BridgeError("Multicast must be enabled with an explicit group allowlist")
        self.secret = secret
        self.verifier = FrameVerifier(secret, self.groups)
        self._selector = selectors.DefaultSelector()
        self._sockets = {}
        self._recent: OrderedDict[tuple[tuple[str, int], bytes], float] = OrderedDict()
        self._limit = _RateLimit()
        self._stop = threading.Event()
        self.stats = {"outbound": 0, "inbound": 0, "rejected": 0, "suppressed": 0}
        self._tunnel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._tunnel.bind((self.tunnel_bind, self.tunnel_port))
            self._tunnel.setblocking(False)
            self._selector.register(self._tunnel, selectors.EVENT_READ, None)
            for selected in self.groups:
                addr, port = selected
                listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    listener.bind(("0.0.0.0", port))
                    membership = socket.inet_aton(addr) + socket.inet_aton(self.interface)
                    listener.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
                    listener.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                        socket.inet_aton(self.interface))
                    listener.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
                                        255 if selected == MDNS else 1)
                    listener.setblocking(False)
                    self._sockets[selected] = listener
                    self._selector.register(listener, selectors.EVENT_READ, selected)
                except BaseException:
                    listener.close()
                    raise
        except BaseException:
            self.stop()
            raise

    def _suppressed(self, selected, payload):
        now = time.monotonic()
        for key, tick in list(self._recent.items()):
            if tick < now - HASH_TTL: self._recent.pop(key, None)
        key = (selected, hashlib.sha256(payload).digest())
        if key in self._recent:
            self.stats["suppressed"] += 1
            return True
        self._recent[key] = now
        while len(self._recent) > 512: self._recent.popitem(last=False)
        return False

    def run(self):
        try:
            LOG.info("Multicast relay enabled on %s for %r", self.interface, self.groups)
            while not self._stop.is_set():
                try: events = self._selector.select(timeout=.25)
                except (OSError, ValueError): break
                for key, _ in events:
                    sock, selected = key.fileobj, key.data
                    try: payload, source = sock.recvfrom(HEADER.size + MAX_PAYLOAD + 17)
                    except OSError: continue
                    if selected is None:
                        if source != self.peer:
                            self.stats["rejected"] += 1
                            continue
                        try: group, data = self.verifier.open(payload)
                        except BridgeError:
                            self.stats["rejected"] += 1
                            continue
                        if self._suppressed(group, data): continue
                        try: self._sockets[group].sendto(data, group)
                        except OSError: self.stats["rejected"] += 1
                        else: self.stats["inbound"] += 1
                    elif (0 < len(payload) <= MAX_PAYLOAD and self._limit.take()
                          and not self._suppressed(selected, payload)):
                        try: self._tunnel.sendto(seal(self.secret, *selected, payload), self.peer)
                        except OSError: self.stats["rejected"] += 1
                        else: self.stats["outbound"] += 1
        finally:
            self.stop()

    def stop(self):
        self._stop.set()
        for item in getattr(self, "_sockets", {}).values():
            item.close()
        if hasattr(self, "_tunnel"): self._tunnel.close()
        if hasattr(self, "_selector"): self._selector.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", required=True, help="LAN IPv4 on Windows or WSL IPv4 inside the sandbox")
    parser.add_argument("--bind", required=True, help="Host WSL-side IPv4 or sandbox IPv4 for the tunnel")
    parser.add_argument("--tunnel-port", type=int, default=48778)
    parser.add_argument("--peer", required=True, help="Pinned remote IPv4:port, never public WAN")
    parser.add_argument("--secret", required=True)
    parser.add_argument("--mdns", action="store_true", help="Enable mDNS 224.0.0.251:5353")
    parser.add_argument("--group", action="append", default=[], help="Additional approved IPv4 multicast GROUP:PORT")
    args = parser.parse_args(argv)
    group_list = channels(mdns=args.mdns, groups=args.group)
    if not group_list:
        parser.error("Multicast is disabled by default; provide --mdns and/or --group")
    try:
        peer_ip, peer_port = channel_peer(args.peer)
        service = MulticastRelay(interface=args.interface, tunnel_bind=args.bind,
            tunnel_port=args.tunnel_port, peer_ip=peer_ip, peer_port=peer_port,
            secret=secret_file(args.secret), groups=group_list)
        service.run()
    except KeyboardInterrupt:
        return 0
    except (BridgeError, OSError) as error:
        LOG.error("Multicast relay failed: %s", type(error).__name__)
        return 1
    return 0


def channel_peer(value: str) -> tuple[str, int]:
    if type(value) is not str or ":" not in value:
        raise BridgeError("Expected a pinned IPv4:PORT tunnel peer")
    address, port = value.rsplit(":", 1)
    try:
        return valid_ipv4(address), valid_port(int(port))
    except (ValueError, TypeError):
        raise BridgeError("Expected a pinned IPv4:PORT tunnel peer") from None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
