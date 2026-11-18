"""Public Operator-to-libp2p identity bindings; discovery grants no authority.

The provider multihash names the raw base public-key block. A provider is only
a candidate: its authenticated transport peer and this signed, expiring record
must both match the independently selected Operator and network.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PROFILE = "syn.operator-peer-record/1"
DOMAIN = b"syn.operator.peer-record/1\0"
MAX_RECORD_BYTES = 8192
MAX_ADDRESSES = 16
MAX_TTL_SECONDS = 3600
CLOCK_SKEW_SECONDS = 60
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BODY_FIELDS = {"profile", "operator", "network_id", "peer_id", "transport_public_key",
                "addresses", "issued_at", "expires_at"}


def canonical(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("ascii")
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("Invalid public peer record JSON") from error


def _hex(value, size, label):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{" + str(size * 2) + "}", value) is None:
        raise ValueError("Invalid canonical " + label)
    return bytes.fromhex(value)


def provider_key(operator_hash: str) -> bytes:
    """SHA2-256 multihash of the Operator's raw 32-byte base public key."""
    return b"\x12\x20" + _hex(operator_hash, 32, "Operator hash")


def provider_cid(operator_hash: str) -> str:
    """CIDv1/raw for the public-key block, in canonical base32 multibase."""
    return "b" + base64.b32encode(b"\x01\x55" + provider_key(operator_hash)).decode("ascii").lower().rstrip("=")


def transport_peer_id(public_key: bytes) -> str:
    """libp2p identity multihash of the standard Ed25519 PublicKey protobuf.

    The protobuf is Type=Ed25519 (field 1, value 1), Data=32 raw bytes
    (field 2). Its 36-byte encoding is small enough for identity inlining.
    This is wire encoding only; signatures use the external Ed25519 library.
    """
    if type(public_key) is not bytes or len(public_key) != 32:
        raise ValueError("Transport public key must be 32-byte Ed25519")
    raw = b"\x00\x24\x08\x01\x12\x20" + public_key
    value, encoded = int.from_bytes(raw, "big"), ""
    while value:
        value, digit = divmod(value, 58)
        encoded = _BASE58[digit] + encoded
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + encoded


def _address(value, peer_id):
    if type(value) is not str or len(value) > 256:
        raise ValueError("Invalid bounded peer multiaddress")
    parts = value.split("/")
    if len(parts) not in (5, 7) or parts[0] or parts[1] not in {"ip4", "ip6"} or parts[3] != "tcp":
        raise ValueError("Peer record requires an IP/TCP multiaddress")
    try:
        address = ipaddress.ip_address(parts[2])
        port = int(parts[4])
    except ValueError as error:
        raise ValueError("Invalid peer IP address or TCP port") from error
    if (str(address) != parts[2] or "%" in parts[2] or parts[1] != "ip" + str(address.version)
            or address.is_unspecified or address.is_multicast
            or str(port) != parts[4] or not 1 <= port <= 65535
            or (len(parts) == 7 and (parts[5] != "p2p" or parts[6] != peer_id))):
        raise ValueError("Noncanonical or mismatched peer multiaddress")


def validate_body(body, *, expected_operator, expected_network=None, now=None):
    """Validate the fixed public attestation grammar, independently of its signer.

    Private unicast addresses are allowed here for explicit LAN/lab use. The
    public Amino transport applies its own globally-routable address policy.
    """
    if type(body) is not dict or set(body) != _BODY_FIELDS or len(canonical(body)) > MAX_RECORD_BYTES:
        raise ValueError("Unexpected public peer record fields or size")
    _hex(expected_operator, 32, "expected Operator hash")
    if body["profile"] != PROFILE or body["operator"] != expected_operator:
        raise ValueError("Peer record differs from the pinned Operator")
    network = body["network_id"]
    if (type(network) is not str or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", network) is None
            or (expected_network is not None and network != expected_network)):
        raise ValueError("Peer record differs from the expected network")
    public = _hex(body["transport_public_key"], 32, "transport public key")
    if body["peer_id"] != transport_peer_id(public):
        raise ValueError("Peer ID differs from the transport public key")
    addresses = body["addresses"]
    if (type(addresses) is not list or not 1 <= len(addresses) <= MAX_ADDRESSES
            or any(type(value) is not str for value in addresses)
            or addresses != sorted(set(addresses))):
        raise ValueError("Peer addresses must be a bounded sorted unique array")
    for address in addresses:
        _address(address, body["peer_id"])
    issued, expires = body["issued_at"], body["expires_at"]
    current = int(time.time()) if now is None else now
    if (type(current) is not int or not 0 <= current < 2**63
            or type(issued) is not int or type(expires) is not int
            or not 0 <= issued < expires < 2**63
            or expires - issued > MAX_TTL_SECONDS
            or issued > current + CLOCK_SKEW_SECONDS or expires <= current):
        raise ValueError("Peer record is expired or has an invalid validity interval")
    return body


def encode(packet) -> bytes:
    raw = canonical(packet)
    if not 0 < len(raw) <= MAX_RECORD_BYTES:
        raise ValueError("Peer record exceeds its size bound")
    return raw


def decode(raw: bytes):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RECORD_BYTES:
        raise ValueError("Peer record exceeds its size bound")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate peer record field")
            value[key] = item
        return value
    try:
        packet = json.loads(raw.decode("ascii"), object_pairs_hook=unique)
        if type(packet) is not dict or canonical(packet) != raw:
            raise ValueError("Peer record must be canonical JSON")
    except (UnicodeError, RecursionError, TypeError) as error:
        raise ValueError("Malformed public peer record") from error
    return packet


def verify(packet, *, expected_operator, expected_network, expected_peer_id=None, now=None):
    """Verify an Operator record; callers pin the authenticated stream peer too.

    This function grants no membership, enrollment, private-mesh or job rights.
    Provider results remain untrusted until this verification succeeds.
    """
    if type(packet) is not dict or set(packet) != {"body", "public_key", "signature"}:
        raise ValueError("Unexpected public peer envelope fields")
    encode(packet)
    body = validate_body(packet["body"], expected_operator=expected_operator,
                         expected_network=expected_network, now=now)
    if expected_peer_id is not None and body["peer_id"] != expected_peer_id:
        raise ValueError("Peer record differs from the authenticated transport peer")
    public = _hex(packet["public_key"], 32, "Operator base public key")
    signature = _hex(packet["signature"], 64, "Operator signature")
    if hashlib.sha256(public).hexdigest() != expected_operator:
        raise ValueError("Public key differs from the pinned Operator base hash")
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, DOMAIN + canonical(body))
    except (InvalidSignature, ValueError) as error:
        raise ValueError("Operator peer record signature failed") from error
    return body


def issue(server, transport_public_key: bytes, addresses: list[str], *, now=None,
          ttl: int = MAX_TTL_SECONDS):
    """Issue from the existing native Operator; never obtain its private key."""
    current = int(time.time()) if now is None else now
    if type(ttl) is not int or not 1 <= ttl <= MAX_TTL_SECONDS:
        raise ValueError("Peer record TTL exceeds its bound")
    if type(addresses) is not list or any(type(value) is not str for value in addresses):
        raise ValueError("Peer addresses must be a string array")
    public = server.signer.public_key
    if type(public) is not bytes or len(public) != 32 or hashlib.sha256(public).hexdigest() != server.expected_hash:
        raise ValueError("Operator signer differs from the installed base identity")
    body = {"profile": PROFILE, "operator": server.expected_hash,
            "network_id": server.config["network_id"],
            "peer_id": transport_peer_id(transport_public_key),
            "transport_public_key": transport_public_key.hex(),
            "addresses": sorted(set(addresses)), "issued_at": current,
            "expires_at": current + ttl}
    validate_body(body, expected_operator=server.expected_hash,
                  expected_network=server.config["network_id"], now=current)
    native = getattr(server.signer, "sign_peer_record", None)
    signature = native(body) if callable(native) else server.signer.sign(DOMAIN + canonical(body))
    if type(signature) is not bytes or len(signature) != 64:
        raise ValueError("Operator peer signature has the wrong encoding")
    packet = {"body": body, "public_key": public.hex(), "signature": signature.hex()}
    verify(packet, expected_operator=server.expected_hash, expected_network=server.config["network_id"],
           expected_peer_id=body["peer_id"], now=current)
    return packet


class OperatorRecordSigner:
    """Small adapter injected into the transport after native readiness."""
    def __init__(self, server, *, ttl=MAX_TTL_SECONDS):
        self.server, self.ttl = server, ttl

    def issue(self, transport_public_key, addresses, *, now=None):
        return issue(self.server, transport_public_key, addresses, now=now, ttl=self.ttl)
