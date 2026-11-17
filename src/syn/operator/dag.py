"""Signed, append-only PostgreSQL DAG projection of private Operator journals.

The OS-anchored authority/adoption journals remain the source of truth. A DAG
checkpoint attests to the *current projection*, not to an historical event time.
No private HD material, raw hardware IDs, or unapproved keys enter this store.
"""
from __future__ import annotations

import hashlib
import json
from sqlalchemy import BigInteger, Column, MetaData, String, Table, Text, UniqueConstraint, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

NODE_DOMAIN = b"syn.operator.dag.node/1\0"
CHECKPOINT_DOMAIN = b"syn.operator.dag.checkpoint/1\0"
metadata = MetaData()

nodes = Table(
    "operatorDagNodes", metadata,
    Column("digest", String(64), primary_key=True),
    Column("operatorHash", String(64), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("source", String(16), nullable=False),
    Column("sourceIndex", BigInteger, nullable=False),
    Column("record", Text, nullable=False),
    UniqueConstraint("operatorHash", "sequence", name="operatorDagNodeSequenceUnique"),
    UniqueConstraint("operatorHash", "source", "sourceIndex", name="operatorDagSourceUnique"),
)
checkpoints = Table(
    "operatorDagCheckpoints", metadata,
    Column("operatorHash", String(64), primary_key=True),
    Column("sequence", BigInteger, primary_key=True),
    Column("root", String(64), nullable=False),
    Column("signature", String(128), nullable=False),
    Column("publicKey", String(64), nullable=False),
    Column("networkId", String(64), nullable=False),
    Column("body", Text, nullable=False),
)


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      allow_nan=False, separators=(",", ":")).encode("ascii")


def digest(value) -> str:
    return hashlib.sha256(NODE_DOMAIN + canonical(value)).hexdigest()


def merkle_root(leaves: list[str]) -> str:
    """RFC 6962-style tree, with distinct leaf and internal node domains."""
    if not leaves:
        return hashlib.sha256(b"").hexdigest()
    def subtree(data):
        if len(data) == 1:
            return hashlib.sha256(b"\x00" + bytes.fromhex(data[0])).digest()
        split = 1 << ((len(data) - 1).bit_length() - 1)
        return hashlib.sha256(b"\x01" + subtree(data[:split]) + subtree(data[split:])).digest()
    return subtree(leaves).hex()


def _source_records(server):
    """Recheck authenticated source state before projecting any history."""
    authority = server.state.read()
    from .adoption_state import EnrollmentJournal
    from .custody import _native_backend
    journal = EnrollmentJournal(server.directory, server.config["keyring_service"],
                                server.config["keyring_account"], server.expected_hash)
    # Never manufacture an absent enrollment journal; older Operators can have
    # authority reservations without an initialized enrollment journal.
    if journal.path.exists() or journal.path.is_symlink():
        events = journal.read()["events"]
    else:
        if _native_backend().get_password(journal.service, journal.slot) is not None:
            raise ValueError("Missing Operator adoption journal has an existing keystore anchor")
        events = []
    return {"authority": authority["reservations"], "adoption": events}


def _rows(session, identity):
    return session.execute(select(nodes).where(nodes.c.operatorHash == identity)
                           .order_by(nodes.c.sequence)).mappings().all()


def _checkpoint_body(identity, network, sequence, root, previous):
    return {"profile": "syn.operator-dag-checkpoint/1", "operator": identity,
            "network_id": network, "sequence": sequence, "root": root,
            "previous_checkpoint": previous}


def _verify_records(rows, sources, identity, network):
    seen = {"authority": set(), "adoption": set()}
    graph = []
    offer_heads = {}
    last = None
    for pos, row in enumerate(rows, 1):
        body = json.loads(row["record"])
        if canonical(body).decode("ascii") != row["record"]:
            raise ValueError("Noncanonical Operator DAG record")
        if row["sequence"] != pos or row["operatorHash"] != identity or row["digest"] != digest(body):
            raise ValueError("Operator DAG digest, sequence or identity changed")
        if body.get("profile") != "syn.operator-dag-node/1" or body.get("operator") != identity or body.get("network_id") != network:
            raise ValueError("Operator DAG network/profile changed")
        source, index = row["source"], row["sourceIndex"]
        if source not in sources or type(index) is not int or index < 0 or index >= len(sources[source]):
            raise ValueError("Operator DAG refers to an absent source event")
        if (source, index) != (body.get("source"), body.get("source_index")) or body.get("event") != sources[source][index]:
            raise ValueError("Operator DAG differs from keystore-verified journal")
        if index in seen[source] or index != len(seen[source]):
            raise ValueError("Operator DAG contains duplicate or reordered source history")
        seen[source].add(index)
        offer = body["event"].get("offer") if source == "adoption" else body["event"].get("pledge_offer_sha256")
        expected = [last] if last else []
        if offer and offer in offer_heads and offer_heads[offer] not in expected:
            expected.append(offer_heads[offer])
        if body.get("parents") != expected:
            raise ValueError("Operator DAG has missing or noncausal parents")
        last = row["digest"]
        if offer:
            offer_heads[offer] = last
        graph.append(last)
    return seen, graph, last, offer_heads


def sync(sessions, server, *, schedulerKey=None):
    """Idempotently reconcile and attest one Operator DAG from private journals.

    The DB transaction either commits every added leaf and its new signed
    checkpoint or none. Refuses divergent source history; never rewrites it.
    """
    identity, network = server.expected_hash, server.config["network_id"]
    sources = _source_records(server)
    public = server.signer.public_key
    if hashlib.sha256(public).hexdigest() != identity:
        raise ValueError("Operator DAG signer differs from installed identity")
    with sessions.begin() as session:
        dialect = session.get_bind().dialect.name
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        if dialect == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                            {"key": "syn.operator.dag:" + identity})
        session.execute(insert(checkpoints).values(
            operatorHash=identity, sequence=0, root=merkle_root([]), signature="0" * 128,
            publicKey=public.hex(), networkId=network,
            body=canonical(_checkpoint_body(identity, network, 0, merkle_root([]), None)).decode("ascii")
        ).on_conflict_do_nothing(index_elements=["operatorHash", "sequence"]))
        rows = _rows(session, identity)
        seen, graph, last, offer_heads = _verify_records(rows, sources, identity, network)
        all_checkpoints = session.execute(select(checkpoints).where(
            checkpoints.c.operatorHash == identity).order_by(checkpoints.c.sequence)).mappings().all()
        previous_checkpoint_hash = None
        previous_checkpoint_sequence = -1
        for checkpoint in all_checkpoints:
            saved_body = json.loads(checkpoint["body"])
            if canonical(saved_body).decode("ascii") != checkpoint["body"]:
                raise ValueError("Noncanonical Operator DAG checkpoint")
            expected_root = merkle_root(graph[:checkpoint["sequence"]])
            if (checkpoint["sequence"] <= previous_checkpoint_sequence or checkpoint["sequence"] > len(graph)
                    or checkpoint["root"] != expected_root or checkpoint["publicKey"] != public.hex()
                    or checkpoint["networkId"] != network):
                raise ValueError("Operator DAG checkpoint differs from retained graph")
            expected_body = _checkpoint_body(identity, network, checkpoint["sequence"],
                                             expected_root, previous_checkpoint_hash)
            if saved_body != expected_body:
                raise ValueError("Operator DAG checkpoint chain or payload changed")
            if checkpoint["sequence"]:
                Ed25519PublicKey.from_public_bytes(public).verify(
                    bytes.fromhex(checkpoint["signature"]), CHECKPOINT_DOMAIN + canonical(saved_body))
            elif checkpoint["signature"] != "0" * 128:
                raise ValueError("Operator DAG genesis signature changed")
            previous_checkpoint_hash = hashlib.sha256(CHECKPOINT_DOMAIN + canonical(saved_body)
                                                      + bytes.fromhex(checkpoint["signature"])).hexdigest()
            previous_checkpoint_sequence = checkpoint["sequence"]
        if not all_checkpoints or previous_checkpoint_sequence != len(rows):
            raise ValueError("Operator DAG checkpoint history is incomplete")
        # Preserve a deterministic replay order for incremental per-source logs;
        # actual chronology across sources is not fabricated.
        added = 0
        for source in ("authority", "adoption"):
            for index in range(len(seen[source]), len(sources[source])):
                event = sources[source][index]
                offer = event.get("offer") if source == "adoption" else event.get("pledge_offer_sha256")
                parents = [last] if last else []
                if offer and offer in offer_heads and offer_heads[offer] not in parents:
                    parents.append(offer_heads[offer])
                body = {"profile": "syn.operator-dag-node/1", "operator": identity,
                        "network_id": network, "source": source, "source_index": index,
                        "event": event, "parents": parents}
                node_hash = digest(body)
                session.execute(nodes.insert().values(
                    digest=node_hash, operatorHash=identity,
                    sequence=len(graph) + 1, source=source, sourceIndex=index,
                    record=canonical(body).decode("ascii")))
                last = node_hash
                graph.append(last)
                if offer:
                    offer_heads[offer] = last
                added += 1
        if added:
            new_root = merkle_root(graph)
            body = _checkpoint_body(identity, network, len(graph), new_root, previous_checkpoint_hash)
            signature = server.signer.sign(CHECKPOINT_DOMAIN + canonical(body))
            Ed25519PublicKey.from_public_bytes(public).verify(signature, CHECKPOINT_DOMAIN + canonical(body))
            session.execute(checkpoints.insert().values(
                operatorHash=identity, sequence=len(graph), root=new_root,
                publicKey=public.hex(), networkId=network,
                signature=signature.hex(), body=canonical(body).decode("ascii")))
        return {"operator": identity, "nodes": len(graph), "added": added,
                "root": merkle_root(graph), "signed": bool(graph)}


def run_sync(databaseUrl, configPath, *, schedulerKey=None):
    """Process-safe scheduler entry point; never capture live Operator objects."""
    from pathlib import Path
    from syn.database import createEngine, createSessionFactory
    from syn.operator.service import OperatorServer
    engine = createEngine(databaseUrl, migrate=False)
    try:
        return sync(createSessionFactory(engine), OperatorServer(Path(configPath)),
                    schedulerKey=schedulerKey)
    finally:
        engine.dispose()
