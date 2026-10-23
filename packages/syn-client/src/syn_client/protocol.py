"""Versioned canonical message receipts. Hashes provide integrity, not consensus."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from uuid import UUID

GENESIS_HASH = "0" * 64
MAX_PAYLOAD_BYTES = 65_536
MAX_RESPONSE_BYTES = 16_777_216
_CHANNEL = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "workspace_id",
        "sequence",
        "message_id",
        "channel",
        "sender",
        "payload",
        "recorded_at",
        "previous_hash",
        "hash",
    }
)


class SynError(RuntimeError):
    """Safe service failure; never contains a token or remote response body."""


class IntegrityError(SynError):
    pass


class MessageConflictError(SynError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def uuid_text(value: str) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Identity must be a canonical UUID")
    return value


def validate_message(message: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(message, dict) or set(message) != {
        "message_id",
        "channel",
        "sender",
        "payload",
    }:
        raise ValueError("Message requires message_id, channel, sender and payload")
    uuid_text(message["message_id"])
    channel, sender = message["channel"], message["sender"]
    if (
        not isinstance(channel, str)
        or len(channel) > 128
        or not _CHANNEL.fullmatch(channel)
    ):
        raise ValueError("Invalid message channel")
    if (
        not isinstance(sender, str)
        or not 1 <= len(sender) <= 128
        or any(ord(c) < 33 or ord(c) > 126 for c in sender)
    ):
        raise ValueError("Sender must be a bounded printable identity")
    if not isinstance(message["payload"], dict):
        raise ValueError("Payload must be a JSON object")
    if len(canonical(message["payload"])) > MAX_PAYLOAD_BYTES:
        raise ValueError("Payload exceeds 65536 canonical bytes")
    # Detach caller-owned nested dictionaries before computing or storing hashes.
    return json.loads(canonical(message))


def receipt_hash(receipt: dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical({k: v for k, v in receipt.items() if k != "hash"})
    ).hexdigest()


def verify_receipt(receipt: dict[str, Any], workspace_id: str) -> None:
    try:
        if (
            set(receipt) != RECEIPT_FIELDS
            or receipt["schema_version"] != 1
            or type(receipt["schema_version"]) is not int
        ):
            raise ValueError("Unsupported receipt")
        if receipt["workspace_id"] != uuid_text(workspace_id):
            raise ValueError("Wrong workspace")
        if (
            type(receipt["sequence"]) is not int
            or not 1 <= receipt["sequence"] <= 9_007_199_254_740_991
        ):
            raise ValueError("Invalid sequence")
        validate_message(
            {
                key: receipt[key]
                for key in ("message_id", "channel", "sender", "payload")
            }
        )
        if not isinstance(receipt["recorded_at"], str) or not receipt[
            "recorded_at"
        ].endswith("+00:00"):
            raise ValueError("Invalid timestamp")
        if not _HASH.fullmatch(receipt["previous_hash"]) or not _HASH.fullmatch(
            receipt["hash"]
        ):
            raise ValueError("Invalid digest")
        if receipt_hash(receipt) != receipt["hash"]:
            raise ValueError("Changed receipt")
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        raise IntegrityError("Invalid Syn message receipt") from error
