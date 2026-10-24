"""Public verification of protected DAG signatures and independent reservations.

The operator's anchor is separate from Agent backups. Receipts grant exactly one
event, permanently; neither old vaults nor generic Agent signatures can reset it.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .governance import ApprovalAuthority

from .consensus import (
    NodeKeyHash,
    PolicyError,
    _digest,
    _fields,
    _revision,
    _uuid,
    canonical_bytes,
    digest_text,
)
from .custody import _b64, _identity, _integer, _signed, verify_ed25519, verify_membership


def _verify(data: dict[str, Any], descriptor: object, prefix: str) -> None:
    _, key = _identity(descriptor)
    verify_ed25519(key, _b64(data["signature"], 64), _signed(prefix.encode("ascii"), data))


def validate_dag_body(body: object) -> dict[str, Any]:
    b = _fields(
        body,
        {
            "profile",
            "context_digest",
            "creator",
            "sequence",
            "self_parent",
            "other_parent",
            "timestamp_ms",
            "proposal",
        },
    )
    if (
        b["profile"] != "syn.hashgraph-event/2"
        or not canonical_bytes(b).isascii()
        or len(canonical_bytes(b)) > 16 * 1024
    ):
        raise PolicyError("Unsupported protected DAG body")
    digest_text(b["context_digest"])
    NodeKeyHash(b["creator"])
    _revision(b["sequence"])
    for parent in (b["self_parent"], b["other_parent"]):
        if parent is not None:
            digest_text(parent)
    if (b["sequence"] == "1") != (b["self_parent"] is None):
        raise PolicyError("Protected DAG sequence requires its exact self-parent")
    _integer(b["timestamp_ms"], 2**52 - 1)

    def native_subset(value: Any) -> bool:
        if type(value) is int:
            return value >= 0
        if isinstance(value, dict):
            return all(native_subset(v) for v in value.values())
        if isinstance(value, list):
            return all(native_subset(v) for v in value)
        return True

    if not native_subset(b):
        raise PolicyError("Protected native DAG bodies require nonnegative protocol integers")
    return b


def protected_event_digest(event: dict[str, Any]) -> str:
    # The reservation signs this content digest; including it would be circular.
    return _digest("syn.hashgraph-event/2", {"body": event["body"], "proof": event["proof"]})


def verify_governance_candidate(
    value: object,
    *,
    governance: ApprovalAuthority,
    previous_record_digest: str,
    successor_sequence: str,
) -> str:
    """A former epoch's valid governor certificate must not win this epoch."""
    digest_text(previous_record_digest)
    _revision(successor_sequence)
    p = _fields(
        value,
        {
            "proposal_digest",
            "type_governance",
            "sequence",
            "previous_record_digest",
            "state_digest",
        },
    )
    digest_text(p["proposal_digest"])
    digest_text(p["state_digest"])
    _revision(p["sequence"])
    if (
        p["sequence"] != successor_sequence
        or p["previous_record_digest"] != previous_record_digest
        or p["proposal_digest"]
        != _digest(
            "syn.policy-proposal/2",
            {
                "sequence": p["sequence"],
                "previous_record_digest": previous_record_digest,
                "state_digest": p["state_digest"],
            },
        )
    ):
        raise PolicyError("Governance candidate belongs to another predecessor or sequence")
    payload = _digest(
        "syn.governance-authorization/1",
        dict(
            role="type-governance",
            proposal_digest=p["proposal_digest"],
            authority_digest=governance.digest,
            policy_digest=None,
        ),
    )
    governance.verify(p["type_governance"], payload, None)
    return digest_text(p["proposal_digest"])


def verify_reservation_intent(value: object, *, membership: dict[str, Any]) -> dict[str, Any]:
    i = _fields(
        value,
        {
            "schema_version",
            "profile",
            "membership",
            "reservation_id",
            "context_digest",
            "sequence",
            "self_parent",
            "timestamp_ms",
            "payload_sha256",
            "event_digest",
            "capsule",
            "signature",
        },
    )
    if (
        type(i["schema_version"]) is not int
        or i["schema_version"] != 1
        or i["profile"] != "syn-dag-reservation-intent-v1"
        or canonical_bytes(i["membership"]) != canonical_bytes(membership)
    ):
        raise PolicyError("Reservation has a different profile or assigned account")
    verify_membership(membership)
    _uuid(i["reservation_id"])
    _revision(i["sequence"])
    for field in ("context_digest", "payload_sha256", "event_digest"):
        digest_text(i[field])
    if i["self_parent"] is not None:
        digest_text(i["self_parent"])
    if (i["sequence"] == "1") != (i["self_parent"] is None):
        raise PolicyError("Reservation sequence and self-parent disagree")
    _integer(i["timestamp_ms"], 2**52 - 1)
    capsule = _fields(i["capsule"], {"nonce_b64", "ciphertext_b64"})
    _b64(capsule["nonce_b64"], 12)
    try:
        raw = base64.b64decode(capsule["ciphertext_b64"], validate=True)
        if (
            not 16 <= len(raw) <= 32 * 1024
            or base64.b64encode(raw).decode() != capsule["ciphertext_b64"]
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise PolicyError("Invalid protected recovery capsule") from None
    _verify(i, membership["agent"], "syn-dag-reservation-intent-v1")
    return i


def verify_anchor_receipt(value: object, *, membership: dict[str, Any]) -> dict[str, Any]:
    r = _fields(value, {"schema_version", "profile", "intent", "signature"})
    if (
        type(r["schema_version"]) is not int
        or r["schema_version"] != 1
        or r["profile"] != "syn-dag-anchor-receipt-v1"
    ):
        raise PolicyError("Unsupported independent DAG reservation")
    verify_reservation_intent(r["intent"], membership=membership)
    _verify(r, membership["operator"], "syn-dag-anchor-receipt-v1")
    return r


def verify_protected_event(value: object, *, membership: dict[str, Any]) -> dict[str, Any]:
    e = _fields(value, {"body", "proof", "reservation"})
    b = validate_dag_body(e["body"])
    p = _fields(
        e["proof"],
        {
            "schema_version",
            "profile",
            "membership",
            "reservation_id",
            "payload_sha256",
            "signature",
        },
    )
    if (
        type(p["schema_version"]) is not int
        or p["schema_version"] != 1
        or p["profile"] != "syn-dag-event-proof-v1"
        or canonical_bytes(p["membership"]) != canonical_bytes(membership)
        or b["creator"] != membership["agent"]["signer_hash"]
    ):
        raise PolicyError("Protected event has a different signature profile or account")
    _uuid(p["reservation_id"])
    payload = _digest("syn.hashgraph-event-payload/2", b)
    if p["payload_sha256"] != payload:
        raise PolicyError("Protected event signature has a different body")
    _verify(p, membership["agent"], "syn-dag-event-proof-v1")
    r = verify_anchor_receipt(e["reservation"], membership=membership)
    i = r["intent"]
    if (
        i["reservation_id"] != p["reservation_id"]
        or i["payload_sha256"] != payload
        or i["event_digest"] != protected_event_digest(e)
        or any(i[k] != b[k] for k in ("context_digest", "sequence", "self_parent", "timestamp_ms"))
    ):
        raise PolicyError("Independent reservation does not authorize this exact DAG event")
    return e
