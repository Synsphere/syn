"""Check remote public evidence without trusting transport or a database."""
from __future__ import annotations

import json

from ..operator.dag_evidence import checkpoint_digest, verify_checkpoint, verify_consistency_bundle

MAX_RESPONSE = 131072
OUTCOMES = frozenset({"accepted", "unchanged", "stale", "equivocation", "checkpoint-conflict",
                      "invalid-checkpoint", "invalid-proof", "unavailable", "no-checkpoint"})


class RejectedEvidence(ValueError):
    def __init__(self, outcome):
        if outcome not in OUTCOMES:
            raise ValueError("Unknown evidence result")
        self.outcome = outcome
        super().__init__(outcome)


def strict_json(raw):
    if not isinstance(raw, (bytes, str)) or len(raw) > MAX_RESPONSE:
        raise ValueError("Oversized witness response")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result
    def bad_number(_):
        raise ValueError("Non-finite JSON number")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_number)
    except (RecursionError, UnicodeError) as error:
        raise ValueError("Malformed peer JSON") from error
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def scoped_checkpoint(packet, peer):
    body = verify_checkpoint(packet)
    if body["operator"] != peer.operator or body["network_id"] != peer.network:
        raise ValueError("Remote checkpoint differs from the configured identity/network pin")
    return body


def classify(previous, candidate, peer, bundle=None):
    """Only different *signed roots at equal sizes* are called equivocation.

    An older reply may just be a lagging replica. A bad proof is not, by itself,
    cryptographic evidence that the remote Operator knowingly signed a fork.
    """
    current = scoped_checkpoint(candidate, peer)
    if previous is None:
        if peer.initialCheckpoint is not None and checkpoint_digest(candidate) != peer.initialCheckpoint:
            raise RejectedEvidence("invalid-checkpoint")
        return "accepted"
    old = scoped_checkpoint(previous, peer)
    if candidate == previous:
        return "unchanged"
    if current["sequence"] < old["sequence"]:
        return "stale"
    if current["sequence"] == old["sequence"]:
        return "equivocation" if current["root"] != old["root"] else "checkpoint-conflict"
    if (not isinstance(bundle, dict) or bundle.get("older") != previous
            or bundle.get("newer") != candidate):
        raise RejectedEvidence("invalid-proof")
    try:
        valid = verify_consistency_bundle(bundle, expected_operator=peer.operator)
    except (ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        raise RejectedEvidence("invalid-proof")
    return "accepted"


def verify_equivocation(first, second, peer):
    a, b = scoped_checkpoint(first, peer), scoped_checkpoint(second, peer)
    return a["sequence"] == b["sequence"] and a["root"] != b["root"]
