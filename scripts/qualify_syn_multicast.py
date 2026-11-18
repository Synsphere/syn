#!/usr/bin/env python3
"""Authenticated LAN/WSL multicast relay witness, with unicast challenge receipt.

Run a listener on a *different* LAN/WSL host first and a sender on the other.
No router/bridge/firewall configuration is changed. A success proves delivery
between these two hosts only, not public Internet multicast or mDNS semantics.
The witness secret must be separate from Syn identity or bridge custody keys.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import struct
import sys
import time

PROFILE = "syn.multicast-qualification/1"
DOMAIN = b"syn.multicast-qualification/1\x00"
MAX_WIRE = 512
DEFAULT_GROUP = "239.255.42.99"
DEFAULT_PORT = 49871
ALLOWED_SKEW = 30
_FIELDS = frozenset({"profile", "kind", "group", "port", "nonce", "when"})


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def read_secret(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("InvalidWitnessSecretFile")
    s = path.stat()
    if (not stat.S_ISREG(s.st_mode) or s.st_size not in (32, 64)
            or (os.name != "nt" and (s.st_mode & 0o077 or s.st_uid != os.getuid()))):
        raise ValueError("InvalidWitnessSecretFile")
    raw = path.read_bytes()
    if len(raw) == 64:
        try:
            raw = bytes.fromhex(raw.decode("ascii"))
        except (ValueError, UnicodeError):
            raise ValueError("InvalidWitnessSecretFile") from None
    if len(raw) != 32:
        raise ValueError("InvalidWitnessSecretFile")
    return raw


def _ip(value: str) -> ipaddress.IPv4Address:
    try:
        ip = ipaddress.ip_address(value)
        if type(ip) is not ipaddress.IPv4Address or str(ip) != value:
            raise ValueError
        return ip
    except ValueError:
        raise ValueError("InvalidIPv4InterfaceOrGroup") from None


def validate_route(group: str, interface: str, port: int, *, allow_loopback=False):
    address, source = _ip(group), _ip(interface)
    if (not address.is_multicast or address.is_unspecified
            or source.is_multicast or source.is_unspecified
            or (source.is_loopback and not allow_loopback)
            or type(port) is not int or not 1024 <= port <= 65535):
        raise ValueError("InvalidMulticastRoute")
    return group, interface, port


def make_packet(secret: bytes, *, kind: str, group: str, port: int,
                nonce: str, when: int | None = None) -> bytes:
    if kind not in {"challenge", "receipt"} or type(nonce) is not str:
        raise ValueError("InvalidWitnessPacket")
    try:
        if len(nonce) != 32 or bytes.fromhex(nonce).hex() != nonce:
            raise ValueError
    except ValueError:
        raise ValueError("InvalidWitnessPacket") from None
    body = {"profile": PROFILE, "kind": kind, "group": group, "port": port,
            "nonce": nonce, "when": int(time.time()) if when is None else when}
    wire = _canonical(body)
    tag = hmac.new(secret, DOMAIN + wire, hashlib.sha256).hexdigest()
    result = _canonical({**body, "mac": tag})
    if len(result) > MAX_WIRE:
        raise ValueError("InvalidWitnessPacket")
    return result


def verify_packet(secret: bytes, payload: bytes, *, kind: str, group: str,
                  port: int, nonce: str | None = None, now: int | None = None) -> dict:
    if type(payload) is not bytes or len(payload) > MAX_WIRE:
        raise ValueError("InvalidWitnessPacket")
    try:
        packet = json.loads(payload)
        if type(packet) is not dict or set(packet) != (_FIELDS | {"mac"}):
            raise ValueError
        mac = packet.pop("mac")
        fields = packet
        if (type(mac) is not str or len(mac) != 64
                or fields["profile"] != PROFILE or fields["kind"] != kind
                or type(fields["group"]) is not str or fields["group"] != group
                or type(fields["port"]) is not int or fields["port"] != port
                or type(fields["nonce"]) is not str or len(fields["nonce"]) != 32
                or bytes.fromhex(fields["nonce"]).hex() != fields["nonce"]
                or (nonce is not None and fields["nonce"] != nonce)
                or type(fields["when"]) is not int
                or abs((int(time.time()) if now is None else now) - fields["when"]) > ALLOWED_SKEW):
            raise ValueError
        expected = hmac.new(secret, DOMAIN + _canonical(fields), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("InvalidWitnessPacket") from None
    return fields


def receive_once(secret: bytes, *, group: str, interface: str, port: int, timeout: float):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", port))
        membership = struct.pack("=4s4s", socket.inet_aton(group), socket.inet_aton(interface))
        listener.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                listener.settimeout(remaining)
                try:
                    wire, sender = listener.recvfrom(MAX_WIRE + 1)
                except socket.timeout:
                    return False
                try:
                    parsed = verify_packet(secret, wire, kind="challenge", group=group, port=port)
                except ValueError:
                    continue
                response = make_packet(secret, kind="receipt", group=group,
                                       port=port, nonce=parsed["nonce"])
                listener.sendto(response, sender)
                return True
        finally:
            listener.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, membership)


def send_once(secret: bytes, *, group: str, interface: str, port: int, timeout: float):
    nonce = secrets.token_hex(16)
    packet = make_packet(secret, kind="challenge", group=group, port=port, nonce=nonce)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sender:
        sender.bind((interface, 0))
        sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
        sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sender.settimeout(timeout)
        sender.sendto(packet, (group, port))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            sender.settimeout(remaining)
            try:
                reply, _ = sender.recvfrom(MAX_WIRE + 1)
            except socket.timeout:
                return False
            try:
                verify_packet(secret, reply, kind="receipt", group=group, port=port, nonce=nonce)
                return True
            except ValueError:
                continue


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("listen", "send"))
    parser.add_argument("--secret-file", type=Path, required=True)
    parser.add_argument("--interface", required=True, help="Local IPv4 address of receiving/sending interface")
    parser.add_argument("--group", default=DEFAULT_GROUP)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--allow-loopback", action="store_true", help="Local smoke test only")
    args = parser.parse_args(argv)
    try:
        group, interface, port = validate_route(args.group, args.interface, args.port,
                                                 allow_loopback=args.allow_loopback)
        if not 0.1 <= args.timeout <= 120:
            raise ValueError("InvalidWitnessTimeout")
        secret = read_secret(args.secret_file)
        verified = (receive_once(secret, group=group, interface=interface, port=port,
                                 timeout=args.timeout) if args.action == "listen" else
                    send_once(secret, group=group, interface=interface, port=port,
                              timeout=args.timeout))
    except (OSError, ValueError) as exc:
        # Never expose key paths, host addresses or OS error messages.
        print(json.dumps({"profile": PROFILE, "verified": False,
                          "reason": "WitnessUnavailable" if isinstance(exc, OSError) else
                                    (str(exc) if str(exc).startswith("Invalid") else "InvalidWitnessRequest")},
                         sort_keys=True))
        return 2
    print(json.dumps({"profile": PROFILE, "verified": verified,
                      "challengeVerified": verified,
                      "scope": "specific-LAN-WSL-multicast-path-only",
                      "internetMulticastProved": False,
                      "mode": args.action}, sort_keys=True))
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
