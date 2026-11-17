"""Public-only proof bundles for independently checking Operator DAG history.

No enrollment records, device identifiers, keystore slots or private journal
contents are returned from these functions. The verification API is offline.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import select

from . import dag, dag_proofs


def _checkpoint(row):
    if row is None:
        raise ValueError("Signed Operator DAG checkpoint not found")
    body = json.loads(row["body"])
    if dag.canonical(body).decode("ascii") != row["body"]:
        raise ValueError("Noncanonical DAG checkpoint")
    if (set(body) != {"profile", "operator", "network_id", "sequence", "root", "previous_checkpoint"}
            or body["profile"] != "syn.operator-dag-checkpoint/1"
            or body["operator"] != row["operatorHash"]
            or body["network_id"] != row["networkId"]
            or body["sequence"] != row["sequence"]
            or body["root"] != row["root"]
            or type(body["sequence"]) is not int or body["sequence"] <= 0):
        raise ValueError("Operator DAG checkpoint contents differ from columns")
    public = bytes.fromhex(row["publicKey"])
    if not hmac.compare_digest(hashlib.sha256(public).hexdigest(), body["operator"]):
        raise ValueError("Operator DAG checkpoint public key does not match identity")
    Ed25519PublicKey.from_public_bytes(public).verify(
        bytes.fromhex(row["signature"]), dag.CHECKPOINT_DOMAIN + dag.canonical(body))
    return {"body": body, "public_key": public.hex(), "signature": row["signature"]}


def verify_checkpoint(packet):
    """Validate a self-contained signed checkpoint against an independently pinned identity."""
    if not isinstance(packet, dict) or set(packet) != {"body", "public_key", "signature"}:
        raise ValueError("Unexpected Operator checkpoint fields")
    body = packet["body"]
    if (not isinstance(body, dict) or
            set(body) != {"profile", "operator", "network_id", "sequence", "root", "previous_checkpoint"} or
            body.get("profile") != "syn.operator-dag-checkpoint/1" or
            type(body.get("sequence")) is not int or body["sequence"] <= 0):
        raise ValueError("Invalid signed checkpoint format")
    # Reuse strict digest decoder (prevents accidental normalization).
    dag_proofs._raw(body["root"])
    dag_proofs._raw(body["operator"])
    if body["previous_checkpoint"] is not None:
        dag_proofs._raw(body["previous_checkpoint"])
    if (type(body["network_id"]) is not str or
            len(body["network_id"]) > 64 or
            not body["network_id"] or not body["network_id"].isascii()):
        raise ValueError("Invalid checkpoint network identifier")
    public = bytes.fromhex(packet["public_key"])
    if len(public) != 32 or hashlib.sha256(public).hexdigest() != body["operator"]:
        raise ValueError("Operator public key hash mismatch")
    signature = bytes.fromhex(packet["signature"])
    if len(signature) != 64:
        raise ValueError("Wrong Operator checkpoint signature size")
    Ed25519PublicKey.from_public_bytes(public).verify(signature,
        dag.CHECKPOINT_DOMAIN + dag.canonical(body))
    return body


def checkpoint_digest(packet):
    verify_checkpoint(packet)
    return hashlib.sha256(dag.CHECKPOINT_DOMAIN + dag.canonical(packet["body"])
                          + bytes.fromhex(packet["signature"])).hexdigest()


def _signed_checkpoint(session, operator, sequence=None):
    query = select(dag.checkpoints).where(dag.checkpoints.c.operatorHash == operator,
                                          dag.checkpoints.c.sequence > 0)
    if sequence is None:
        query = query.order_by(dag.checkpoints.c.sequence.desc()).limit(1)
    else:
        query = query.where(dag.checkpoints.c.sequence == sequence)
    return _checkpoint(session.execute(query).mappings().first())


def _digests(session, operator, count):
    if type(count) is not int or count <= 0 or count > dag_proofs.MAX_LEAVES:
        raise ValueError("Invalid bounded checkpoint size")
    query = select(dag.nodes.c.digest, dag.nodes.c.sequence).where(
        dag.nodes.c.operatorHash == operator,
        dag.nodes.c.sequence <= count).order_by(dag.nodes.c.sequence)
    rows = session.execute(query).all()
    if len(rows) != count or any(row.sequence != i for i, row in enumerate(rows, 1)):
        raise ValueError("DAG projection has missing leaves")
    return [row.digest for row in rows]


def latest(sessions, operator):
    with sessions() as session:
        return _signed_checkpoint(session, operator)


def inclusion_bundle(sessions, operator, index):
    """Zero-based inclusion, using the most recent signed checkpoint."""
    with sessions() as session:
        checkpoint = _signed_checkpoint(session, operator)
        count = checkpoint["body"]["sequence"]
        leaves = _digests(session, operator, count)
    if dag_proofs.root(leaves) != checkpoint["body"]["root"]:
        raise ValueError("Operator DAG projection differs from its signed checkpoint")
    return {"profile": "syn.operator-dag-inclusion/1", "checkpoint": checkpoint,
            "leaf_index": index, "leaf_digest": leaves[index] if type(index) is int and 0 <= index < count else None,
            "audit_path": dag_proofs.inclusion(leaves, index)}


def consistency_bundle(sessions, operator, old_size):
    with sessions() as session:
        current = _signed_checkpoint(session, operator)
        prior = _signed_checkpoint(session, operator, old_size)
        leaves = _digests(session, operator, current["body"]["sequence"])
    if (dag_proofs.root(leaves) != current["body"]["root"] or
            dag_proofs.root(leaves[:old_size]) != prior["body"]["root"]):
        raise ValueError("Operator DAG history disagrees with its signed checkpoints")
    return {"profile": "syn.operator-dag-consistency/1", "older": prior,
            "newer": current, "audit_path": dag_proofs.consistency(leaves, old_size)}


def verify_inclusion_bundle(packet, *, expected_operator):
    if (not isinstance(packet, dict) or set(packet) != {
            "profile", "checkpoint", "leaf_index", "leaf_digest", "audit_path"} or
            packet["profile"] != "syn.operator-dag-inclusion/1"):
        raise ValueError("Invalid inclusion bundle")
    body = verify_checkpoint(packet["checkpoint"])
    if body["operator"] != expected_operator:
        raise ValueError("Untrusted Operator identity")
    return dag_proofs.verify_inclusion(packet["leaf_digest"], packet["leaf_index"],
        body["sequence"], body["root"], packet["audit_path"])


def verify_consistency_bundle(packet, *, expected_operator):
    if (not isinstance(packet, dict) or set(packet) != {
            "profile", "older", "newer", "audit_path"} or
            packet["profile"] != "syn.operator-dag-consistency/1"):
        raise ValueError("Invalid consistency bundle")
    old = verify_checkpoint(packet["older"])
    new = verify_checkpoint(packet["newer"])
    if old["operator"] != new["operator"] or old["operator"] != expected_operator:
        raise ValueError("Operator checkpoint identities disagree")
    if (old["network_id"] != new["network_id"] or
            packet["older"]["public_key"] != packet["newer"]["public_key"]):
        raise ValueError("Different Operator/network scope")
    return dag_proofs.verify_consistency(old["sequence"], new["sequence"],
        old["root"], new["root"], packet["audit_path"])
