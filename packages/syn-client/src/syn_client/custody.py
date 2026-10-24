"""Public verification of the frozen native Agent v1 credential/proof formats.

No key generation, private-key import or signing. Crypto is an optional extra.
These signatures prove assignment/consent, not public HD lineage or finality.
"""

from __future__ import annotations

import base64
from typing import Any

from .consensus import NodeKeyHash, PolicyError, _fields, _uuid, canonical_bytes, digest_text


def _b64(value: object, length: int) -> bytes:
    try:
        if not isinstance(value, str):
            raise ValueError
        raw = base64.b64decode(value, validate=True)
        if len(raw) != length or base64.b64encode(raw).decode("ascii") != value:
            raise ValueError
        return raw
    except (ValueError, TypeError):
        raise PolicyError("Invalid native public-key or signature encoding") from None


def verify_ed25519(public_key: bytes, signature: bytes, message: bytes) -> None:
    try:
        from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise PolicyError("Signed governance requires the Syn client governance extra") from None
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, UnsupportedAlgorithm, ValueError, TypeError):
        raise PolicyError("Invalid native Ed25519 approval or membership signature") from None


def _integer(value: object, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise PolicyError("Invalid bounded native protocol integer")
    return value


def _identity(value: object) -> tuple[str, bytes]:
    data = _fields(
        value,
        {
            "type_ref",
            "schema_version",
            "profile_id",
            "hash_algorithm",
            "signer_hash",
            "public_key_algorithm",
            "public_key_encoding",
            "public_key",
        },
    )
    if (
        data["type_ref"] != "synny://type/public-identity-descriptor"
        or type(data["schema_version"]) is not int
        or data["schema_version"] != 2
        or data["profile_id"] != "potbot-identity-v1"
        or data["hash_algorithm"] != "SHA-256"
        or data["public_key_algorithm"] != "Ed25519"
        or data["public_key_encoding"] != "base64"
    ):
        raise PolicyError("Unsupported native account identity")
    key = _b64(data["public_key"], 32)
    if NodeKeyHash.from_public_key(key) != NodeKeyHash(data["signer_hash"]):
        raise PolicyError("Native account key does not match its node hash")
    return data["signer_hash"], key


def _signed(prefix: bytes, data: dict[str, Any]) -> bytes:
    unsigned = {k: v for k, v in data.items() if k != "signature"}
    raw = canonical_bytes(unsigned)
    # Native v1 is ASCII-only. Never reinterpret its signed bytes as a new codec.
    if not raw.isascii() or len(raw) > 65_536:
        raise PolicyError("Unsupported native public document")
    return prefix + b"\0" + raw


def verify_membership(value: object) -> dict[str, Any]:
    data = _fields(
        value,
        {
            "schema_version",
            "profile",
            "operator",
            "agent",
            "account_number",
            "instance_id",
            "group_id",
            "group_epoch",
            "signature",
        },
    )
    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != 1
        or data["profile"] != "syn-agent-account-v1"
    ):
        raise PolicyError("Unsupported native membership")
    issuer, key = _identity(data["operator"])
    node, _ = _identity(data["agent"])
    if issuer == node:
        raise PolicyError("An Agent account cannot be its issuer base key")
    for name in ("account_number", "group_epoch"):
        _integer(data[name], 2**31 - 1)
    for name in ("instance_id", "group_id"):
        _uuid(data[name])
    verify_ed25519(key, _b64(data["signature"], 64), _signed(b"syn-agent-membership-v1", data))
    return data


def verify_agent_approval(
    value: object, *, membership: dict[str, Any], payload_digest: str, now: int | None
) -> str:
    """Require a live proof on admission; None checks historical signature evidence only."""
    data = _fields(
        value,
        {
            "schema_version",
            "profile",
            "membership",
            "request_id",
            "payload_sha256",
            "issued_at",
            "expires_at",
            "signature",
        },
    )
    digest_text(payload_digest)
    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != 1
        or data["profile"] != "syn-agent-proof-v1"
        or data["payload_sha256"] != payload_digest
        or canonical_bytes(data["membership"]) != canonical_bytes(membership)
    ):
        raise PolicyError("Approval does not match its role, proposal or trusted assignment")
    _uuid(data["request_id"])
    issued = _integer(data["issued_at"], 2**31 * 3600 - 1)
    expires = _integer(data["expires_at"], 2**31 * 3600 - 1)
    if not 0 < expires - issued <= 300:
        raise PolicyError("Invalid native approval validity window")
    if now is not None and not issued <= _integer(now, 2**31 * 3600 - 1) < expires:
        raise PolicyError("Approval is outside its admission window")
    checked = verify_membership(data["membership"])
    node, key = _identity(checked["agent"])
    verify_ed25519(key, _b64(data["signature"], 64), _signed(b"syn-agent-proof-v1", data))
    return node
