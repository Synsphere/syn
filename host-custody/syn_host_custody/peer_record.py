"""Independent, dependency-free validation of the public peer signing grammar.

Keep the host boundary independent of the network runtime. Only a fixed public
identity/endpoint statement can use retained approval; this is no generic signer.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time

DOMAIN = b"syn.operator.peer-record/1\0"
PROFILE = "syn.operator-peer-record/1"
APPROVAL_SUFFIX = ".operator.peer-record-approval.v1"
MAX_RECORD_BYTES = 8192
MAX_TTL_SECONDS = 3600
CLOCK_SKEW_SECONDS = 60
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def _public_key(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("Invalid peer transport public key")
    return bytes.fromhex(value)


def _peer_id(public):
    # Standard Ed25519 PublicKey protobuf (36 bytes) in an identity multihash.
    raw = b"\x00\x24\x08\x01\x12\x20" + public
    number, encoded = int.from_bytes(raw, "big"), ""
    while number:
        number, digit = divmod(number, 58)
        encoded = _BASE58[digit] + encoded
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + encoded


def validate_body(body, expected_operator, *, now=None):
    if (type(body) is not dict or set(body) != {"profile", "operator", "network_id", "peer_id",
            "transport_public_key", "addresses", "issued_at", "expires_at"}
            or len(canonical(body)) > MAX_RECORD_BYTES):
        raise ValueError("Invalid host peer record fields or size")
    if (body["profile"] != PROFILE or type(expected_operator) is not str
            or re.fullmatch(r"[0-9a-f]{64}", expected_operator) is None
            or body["operator"] != expected_operator):
        raise ValueError("Peer record differs from the host Operator identity")
    if (type(body["network_id"]) is not str
            or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", body["network_id"]) is None):
        raise ValueError("Invalid peer record network")
    public = _public_key(body["transport_public_key"])
    peer_id = _peer_id(public)
    if body["peer_id"] != peer_id:
        raise ValueError("Peer record transport identity mismatch")
    addresses = body["addresses"]
    if (type(addresses) is not list or not 1 <= len(addresses) <= 16
            or any(type(value) is not str for value in addresses)
            or addresses != sorted(set(addresses))):
        raise ValueError("Invalid canonical peer address array")
    for value in addresses:
        parts = value.split("/")
        if (len(value) > 256 or len(parts) not in (5, 7) or parts[0]
                or parts[1] not in {"ip4", "ip6"} or parts[3] != "tcp"):
            raise ValueError("Peer record requires bounded IP/TCP endpoints")
        address, port = ipaddress.ip_address(parts[2]), int(parts[4])
        if (str(address) != parts[2] or "%" in parts[2] or parts[1] != "ip" + str(address.version)
                or address.is_unspecified or address.is_multicast
                or str(port) != parts[4] or not 1 <= port <= 65535
                or (len(parts) == 7 and (parts[5] != "p2p" or parts[6] != peer_id))):
            raise ValueError("Invalid peer record endpoint or transport binding")
    issued, expires = body["issued_at"], body["expires_at"]
    current = int(time.time()) if now is None else now
    if (type(current) is not int or not 0 <= current < 2**63
            or type(issued) is not int or type(expires) is not int
            or not 0 <= issued < expires < 2**63
            or expires - issued > MAX_TTL_SECONDS
            or issued > current + CLOCK_SKEW_SECONDS or expires <= current):
        raise ValueError("Host peer record has an invalid or expired validity interval")
    return body


def binding_digest(body):
    """Approval is for an exact endpoint binding; only its times may refresh."""
    binding = {key: value for key, value in body.items() if key not in {"issued_at", "expires_at"}}
    return hashlib.sha256(DOMAIN + b"approval\0" + canonical(binding)).hexdigest()


def sign_with_approval(authority, service, account, pin, body, sign):
    validate_body(body, pin)
    slot = account + APPROVAL_SUFFIX
    saved = authority.store.get_slot(service, slot)
    previous = json.loads(saved) if saved is not None else None
    if previous is not None and (
            type(previous) is not dict or set(previous) != {"profile", "operator", "binding"}
            or previous["profile"] != "syn.operator-peer-record-approval/1"
            or previous["operator"] != pin
            or type(previous["binding"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", previous["binding"]) is None
            or canonical(previous).decode("ascii") != saved):
        raise ValueError("Retained peer record approval is invalid")
    commitment = binding_digest(body)
    if previous is None or previous["binding"] != commitment:
        question = ("Publish this Syn Operator's public peer binding?\nOperator: " + pin
                    + "\nNetwork: " + body["network_id"] + "\nTransport peer: " + body["peer_id"]
                    + "\nAddresses:\n" + "\n".join(body["addresses"])
                    + "\nApproval permits bounded refreshes of this exact binding only.")
        if not authority.approve(question):
            raise ValueError("Windows peer record publication approval declined")
        retained = {"profile": "syn.operator-peer-record-approval/1", "operator": pin,
                    "binding": commitment}
        # Approval must be durably retained before the signature can escape.
        authority.store.set_slot(service, slot, canonical(retained).decode("ascii"))
    # A native prompt may have outlived the proposed record.
    validate_body(body, pin)
    return sign(DOMAIN + canonical(body))
