"""Transactional public witness archive, with receipts in the existing node ledger.

Remote journal bodies are never fetched. A witness records observations, not
consensus votes. Independent custody anchors, not DB triggers alone, detect a
rollback of the witness database itself.
"""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import (BigInteger, Boolean, Column, DateTime, MetaData, String,
                        Table, Text, UniqueConstraint, select)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..operator.dag import canonical
from ..operator.dag_evidence import checkpoint_digest
from .protocol import OUTCOMES, RejectedEvidence, classify, scoped_checkpoint

metadata = MetaData()
heads = Table("operatorDagWitnessHeads", metadata,
    Column("observerHash", String(64), primary_key=True),
    Column("peerHash", String(64), primary_key=True),
    Column("networkId", String(64), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("lastRecord", String(64)),
    Column("checkpoint", Text),
    Column("lastOutcome", String(32), nullable=False),
    Column("conflicted", Boolean, nullable=False),
    Column("checkedAt", DateTime(timezone=True), nullable=False))
records = Table("operatorDagWitnessRecords", metadata,
    Column("digest", String(64), primary_key=True),
    Column("observerHash", String(64), nullable=False),
    Column("peerHash", String(64), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("body", Text, nullable=False),
    Column("receiptSequence", BigInteger, nullable=False),
    Column("receiptHash", String(64), nullable=False),
    UniqueConstraint("observerHash", "peerHash", "sequence", name="operatorDagWitnessSequenceUnique"))
RECORD_DOMAIN = b"syn.operator.dag.witness-record/1\0"
MAX_RECORDS = 100000


def record_digest(body):
    return hashlib.sha256(RECORD_DOMAIN + canonical(body)).hexdigest()


class WitnessStore:
    def __init__(self, sessions, observer, node_ref, *, ledger=None):
        from ..operator.dag_proofs import _raw
        _raw(observer)
        if not isinstance(node_ref, str) or not node_ref.startswith("syn://thing/"):
            raise ValueError("Witness archive needs the local node Thing reference")
        self.sessions, self.observer, self.node_ref = sessions, observer, node_ref
        self.workspace = str(uuid5(NAMESPACE_URL, "syn-node-ledger:" + node_ref))
        if ledger is None:
            from syn.communication import MessageStore
            ledger = MessageStore(sessions)
        self.ledger = ledger

    def _where(self, table, peer):
        return (table.c.observerHash == self.observer, table.c.peerHash == peer.operator)

    def _head(self, session, peer, *, lock=False):
        query = select(heads).where(*self._where(heads, peer))
        if lock:
            query = query.with_for_update()
        row = session.execute(query).mappings().first()
        if row is not None and row["networkId"] != peer.network:
            raise ValueError("A retained witness peer cannot silently change network")
        return row

    def _verify(self, session, peer, head):
        saved = session.execute(select(records).where(*self._where(records, peer))
            .order_by(records.c.sequence).limit(MAX_RECORDS + 1)).mappings().all()
        if len(saved) > MAX_RECORDS:
            raise ValueError("Witness archive requires reviewed archival before further growth")
        if head is None:
            if saved:
                raise ValueError("Witness archive head is missing")
            return None, [], None
        previous_record = previous_checkpoint = None
        accepted, last_body, conflict = [], None, False
        for sequence, row in enumerate(saved, 1):
            body = json.loads(row["body"])
            if (canonical(body).decode("ascii") != row["body"]
                    or set(body) != {"profile", "observer", "peer", "network", "sequence", "previous_record",
                                    "outcome", "checkpoint", "previous_checkpoint", "consistency", "observed_at"}
                    or row["sequence"] != sequence or body["sequence"] != sequence
                    or body["observer"] != self.observer or body["peer"] != peer.operator
                    or body["network"] != peer.network or body["profile"] != "syn.operator-dag-witness/1"
                    or body["previous_record"] != previous_record or record_digest(body) != row["digest"]
                    or body["previous_checkpoint"] != previous_checkpoint
                    or body["outcome"] not in OUTCOMES | {"quarantined"}):
                raise ValueError("Witness archive was altered or reordered")
            # Check the receipt in the same database transaction, not merely
            # the archive's copy of its hash. Full shared-chain replay remains
            # available through the node ledger's existing verification tools.
            from syn.communication import messages as ledger_messages
            from syn_client.protocol import verify_receipt
            receipt = session.execute(select(ledger_messages).where(
                ledger_messages.c.workspace_id == self.workspace,
                ledger_messages.c.sequence == row["receiptSequence"])).mappings().first()
            if (receipt is None or receipt["hash"] != row["receiptHash"] or receipt["payload"] != {
                    "profile": "syn.operator-dag-witness-record/1", "digest": row["digest"], "body": body}):
                raise ValueError("Witness evidence differs from its node ledger receipt")
            verify_receipt({"schema_version": 1, **dict(receipt)}, self.workspace)
            outcome = body["outcome"]
            if outcome in {"accepted", "unchanged", "stale", "equivocation", "checkpoint-conflict"}:
                actual = classify(previous_checkpoint, body["checkpoint"], peer, body["consistency"])
                if actual != outcome or (conflict and outcome == "accepted"):
                    raise ValueError("Witness record has an invalid trust transition")
                if outcome == "accepted":
                    previous_checkpoint = body["checkpoint"]
                    accepted.append(body)
            if outcome in {"equivocation", "checkpoint-conflict"}:
                conflict = True
            previous_record, last_body = row["digest"], body
        checkpoint = json.loads(head["checkpoint"]) if head["checkpoint"] is not None else None
        if (head["sequence"] != len(saved) or head["lastRecord"] != previous_record
                or checkpoint != previous_checkpoint or head["conflicted"] != conflict
                or (last_body is not None and head["lastOutcome"] != last_body["outcome"])):
            raise ValueError("Witness head differs from retained evidence")
        return checkpoint, accepted, last_body

    def snapshot(self, peer):
        with self.sessions() as session:
            head = self._head(session, peer)
            checkpoint, accepted, last = self._verify(session, peer, head)
            return {"checkpoint": checkpoint, "accepted": accepted, "last": last,
                    "conflicted": bool(head and head["conflicted"])}

    def record(self, peer, *, previous, candidate=None, consistency=None, failure=None):
        """Commit an observation and its common ledger receipt atomically.

        'previous' is an optimistic guard against a second witness worker changing
        the head while the network request was in flight. Callers retry later.
        """
        if failure is not None and failure not in {"invalid-checkpoint", "invalid-proof", "unavailable", "no-checkpoint"}:
            raise ValueError("Invalid witness failure classification")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
            session.execute(insert(heads).values(observerHash=self.observer, peerHash=peer.operator,
                networkId=peer.network, sequence=0, lastRecord=None, checkpoint=None, lastOutcome="unchanged",
                conflicted=False, checkedAt=now).on_conflict_do_nothing(index_elements=["observerHash", "peerHash"]))
            head = self._head(session, peer, lock=True)
            current, _, last = self._verify(session, peer, head)
            if current != previous:
                return {"status": "head-changed", "recorded": False}
            if failure is not None:
                outcome, candidate, consistency = failure, None, None
            else:
                scoped_checkpoint(candidate, peer)
                try:
                    outcome = classify(current, candidate, peer, consistency)
                except RejectedEvidence as error:
                    outcome, candidate, consistency = error.outcome, None, None
                if head["conflicted"] and outcome == "accepted":
                    outcome, candidate, consistency = "quarantined", None, None
            # Repeated outage and repeated identical checkpoint do not inflate
            # the immutable log. checkedAt is a liveness projection, not proof.
            if last and ((outcome == "unchanged" and last["outcome"] in {"accepted", "unchanged"}) or (last["outcome"] == outcome
                        and last["checkpoint"] == candidate and last["consistency"] == consistency)):
                session.execute(heads.update().where(*self._where(heads, peer)).values(checkedAt=now))
                return {"status": outcome, "recorded": False}
            sequence = head["sequence"] + 1
            if sequence > MAX_RECORDS:
                raise ValueError("Witness archive capacity exceeded")
            body = {"profile": "syn.operator-dag-witness/1", "observer": self.observer,
                "peer": peer.operator, "network": peer.network, "sequence": sequence,
                "previous_record": head["lastRecord"], "outcome": outcome, "checkpoint": candidate,
                "previous_checkpoint": current, "consistency": consistency,
                "observed_at": now.isoformat()}
            commitment = record_digest(body)
            receipt = self.ledger.appendInTransaction(session, self.workspace, {
                "message_id": str(uuid4()), "channel": "node.dag-witness", "sender": "operator-" + self.observer,
                "payload": {"profile": "syn.operator-dag-witness-record/1", "digest": commitment, "body": body}})
            session.execute(records.insert().values(digest=commitment, observerHash=self.observer,
                peerHash=peer.operator, sequence=sequence, body=canonical(body).decode("ascii"),
                receiptSequence=receipt["sequence"], receiptHash=receipt["hash"]))
            updated = candidate if outcome == "accepted" else current
            session.execute(heads.update().where(*self._where(heads, peer)).values(
                sequence=sequence, lastRecord=commitment,
                checkpoint=canonical(updated).decode("ascii") if updated is not None else None,
                lastOutcome=outcome, checkedAt=now,
                conflicted=head["conflicted"] or outcome in {"equivocation", "checkpoint-conflict"}))
            return {"status": outcome, "recorded": True, "digest": commitment,
                    "checkpoint_digest": checkpoint_digest(updated) if updated else None}
