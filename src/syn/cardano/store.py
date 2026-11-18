"""Atomic source-qualified Cardano observations in native objects and two ledgers.

Each peer has its own chain projection. Rollback appends retraction Embers;
it never deletes a block Event or rewrites historical observations. No Cardano
consensus or finality judgement is made by this trusted-node observer.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import json
import re
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select, text

from syn.positions.store import workspace_for
from syn.positions.types import local_ref
from syn.recurrence import utc
from syn_client.protocol import GENESIS_HASH, canonical
from . import schema as db
from .protocol import (MAX_BLOCK_BYTES, MAX_HEADER_BYTES, CardanoProtocolError,
                       decode_block_header)


BLOCKCHAIN_REF = "syn://thing/cardano"
MAX_ROLLBACK_BLOCKS = 4096
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ERAS = {"shelley", "allegra", "mary", "alonzo", "babbage", "conway"}


class CardanoStoreError(ValueError):
    pass


class CardanoStoreConflict(CardanoStoreError):
    """A source or persisted object contradicts retained history."""


def _digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def event_workspace(event_ref: str) -> str:
    return str(uuid5(NAMESPACE_URL, "syn-object-ledger:" + local_ref(event_ref, "event")))


def _point(value):
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"slot", "hash"}:
        raise CardanoStoreError("A chain point requires slot and hash")
    if value == {"slot": None, "hash": None}:
        return None
    if (type(value["slot"]) is not int or not 0 <= value["slot"] < 2**63
            or type(value["hash"]) is not str or _HASH.fullmatch(value["hash"]) is None):
        raise CardanoStoreError("Invalid Cardano chain point")
    return dict(value)


def _observed_at(value):
    value = datetime.now(UTC) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CardanoStoreError("Observation time must have an explicit timezone")
    return value.astimezone(UTC)


def _source(node_ref, network_magic, peer):
    local_ref(node_ref, "thing")
    if type(network_magic) is not int or not 0 <= network_magic <= 4294967295:
        raise CardanoStoreError("Invalid Cardano network magic")
    if (type(peer) is not str or not 1 <= len(peer) <= 512
            or any(ord(c) < 33 or ord(c) > 126 for c in peer)):
        raise CardanoStoreError("Invalid configured Cardano peer")
    return _digest({"profile": "syn.cardano-source/1", "node": node_ref,
                    "network_magic": network_magic, "peer": peer})


class CardanoStore:
    def __init__(self, sessions, *, ledger=None):
        self.sessions = sessions
        if ledger is None:
            from syn.communication import MessageStore
            ledger = MessageStore(sessions)
        self.ledger = ledger

    @contextmanager
    def _transaction(self):
        with self.sessions.begin() as session:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(text("SET LOCAL lock_timeout = '10s'"))
                session.execute(text("SET LOCAL statement_timeout = '30s'"))
            yield session

    def _node_lock(self, session, node_ref):
        row = session.execute(select(db.position_nodes).where(
            db.position_nodes.c.nodeRef == node_ref).with_for_update()).mappings().first()
        if row is None or row["ledgerWorkspace"] != workspace_for(node_ref):
            raise CardanoStoreConflict("Cardano observer requires its prepared Syn node ledger")

    def _state(self, session, node_ref, network_magic, peer):
        self._node_lock(session, node_ref)
        identity = _source(node_ref, network_magic, peer)
        row = session.execute(select(db.followers).where(
            db.followers.c.id == identity).with_for_update()).mappings().first()
        if row is None:
            raise CardanoStoreConflict("Cardano source was not prepared")
        return row

    def prepare(self, node_ref: str, network_magic: int, peers) -> None:
        if type(peers) not in (list, tuple) or not 1 <= len(peers) <= 8 or len(set(peers)) != len(peers):
            raise CardanoStoreError("Expected one to eight distinct configured peers")
        sources = [(peer, _source(node_ref, network_magic, peer)) for peer in peers]
        with self._transaction() as session:
            self._node_lock(session, node_ref)
            for slug, parent, _ in db.TYPE_DEFINITIONS:
                reference = "syn://type/" + slug
                if session.execute(select(db.primitive["types"].c.id).where(
                        db.primitive["types"].c.id == reference)).first() is None:
                    raise CardanoStoreConflict("Cardano schema/types are not installed; run migrations")
                if session.execute(select(db.typeParents).where(
                        db.typeParents.c.childRef == reference,
                        db.typeParents.c.parentRef == "syn://type/" + parent)).first() is None:
                    raise CardanoStoreConflict("Cardano type hierarchy differs from its installed contract")
            thing = session.execute(select(db.primitive["things"]).where(
                db.primitive["things"].c.id == BLOCKCHAIN_REF)).mappings().first()
            if thing is None or thing["name"] != "Cardano" or thing["typeRef"] != "syn://type/blockchain":
                raise CardanoStoreConflict("The Cardano blockchain Thing is absent or has conflicting contents")
            for peer, identity in sources:
                db.insert_ignore(session, db.followers, {"id": identity, "nodeRef": node_ref,
                    "networkMagic": network_magic, "peer": peer, "initialized": False,
                    "point": None, "anchor": None, "revision": 0}, ["id"])

    def intersection_points(self, node_ref, network_magic, peer, limit=64):
        if type(limit) is not int or not 2 <= limit <= 64:
            raise CardanoStoreError("Intersection point limit must be between 2 and 64")
        with self._transaction() as session:
            state = self._state(session, node_ref, network_magic, peer)
            if not state["initialized"]:
                return []
            current = _point(state["point"])
            if current is None:
                return [None]
            points = [current]
            selection = select(db.chain.c.slot, db.chain.c.blockHash).where(
                db.chain.c.followerId == state["id"], db.chain.c.active.is_(True)).order_by(db.chain.c.slot.desc())
            recent = max(1, min(32, limit // 2))
            rows = session.execute(selection.limit(recent)).all()
            for slot, block_hash in rows:
                point = {"slot": slot, "hash": block_hash}
                if point not in points:
                    points.append(point)
            offset = recent
            while len(points) < limit - 1:
                row = session.execute(selection.offset(offset).limit(1)).first()
                if row is None:
                    break
                point = {"slot": row[0], "hash": row[1]}
                if point not in points:
                    points.append(point)
                offset *= 2
            anchor = _point(state["anchor"])
            if anchor not in points:
                points.append(anchor)
            return points[:limit]

    def _lock_targets(self, session, node_ref, event_refs):
        # Serialize object chronicle numbering before acquiring any receipt head.
        # All multi-ledger writers take every head in lexical order; existing
        # position/clock writers lock this same node before its one ledger head.
        if session.get_bind().dialect.name == "postgresql":
            for reference in sorted({node_ref, *event_refs}):
                session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                                {"key": "syn.object-ledger:" + reference})
        from syn.communication import heads
        workspaces = {workspace_for(node_ref), *(event_workspace(ref) for ref in event_refs)}
        for workspace in sorted(workspaces):
            db.insert_ignore(session, heads, {"workspace_id": workspace,
                "sequence": 0, "hash": GENESIS_HASH}, ["workspace_id"])
            session.execute(select(heads).where(heads.c.workspace_id == workspace)
                            .with_for_update()).one()

    def _checkpoint(self, session, state, point, action, at, retracted=0):
        revision = state["revision"] + 1
        payload = {"profile": "syn.cardano-checkpoint/1", "nodeRef": state["nodeRef"],
            "networkMagic": state["networkMagic"], "peer": state["peer"],
            "revision": revision, "action": action, "point": point,
            "previousPoint": state["point"] if state["initialized"] else None,
            "observedAt": at.isoformat(), "retracted": retracted}
        self.ledger.appendInTransaction(session, workspace_for(state["nodeRef"]), {
            "message_id": str(uuid5(NAMESPACE_URL, f"syn-cardano-checkpoint:{state['id']}:{revision}")),
            "channel": "node.cardano", "sender": "node-" + _digest(state["nodeRef"]), "payload": payload})
        return revision

    def intersect(self, node_ref, network_magic, peer, point, *, observed_at=None):
        point, at = _point(point), _observed_at(observed_at)
        with self._transaction() as session:
            state = self._state(session, node_ref, network_magic, peer)
            if state["initialized"]:
                return self._rollback(session, state, point, at, action="intersect")
            self._lock_targets(session, node_ref, [])
            revision = self._checkpoint(session, state, point, "start", at)
            session.execute(db.followers.update().where(db.followers.c.id == state["id"]).values(
                initialized=True, point=point, anchor=point, revision=revision, lastObservedAt=at))
            return {"action": "start", "point": point, "revision": revision, "retracted": 0}

    def _header(self, header, block_cbor):
        required = {"block_hash", "slot", "block_number", "previous_hash", "era", "cbor"}
        if type(header) is not dict or set(header) != required:
            raise CardanoStoreError("Invalid Cardano header fields")
        point = _point({"slot": header["slot"], "hash": header["block_hash"]})
        if (type(header["block_number"]) is not int or not 0 <= header["block_number"] < 2**63
                or header["era"] not in _ERAS):
            raise CardanoStoreError("Invalid or unsupported Cardano header")
        previous = header["previous_hash"]
        if previous is not None and (type(previous) is not str or _HASH.fullmatch(previous) is None):
            raise CardanoStoreError("Invalid Cardano predecessor")
        raw = header["cbor"]
        if (type(raw) is not bytes or not 1 <= len(raw) <= MAX_HEADER_BYTES
                or hashlib.blake2b(raw, digest_size=32).hexdigest() != point["hash"]):
            raise CardanoStoreError("Cardano header bytes differ from their block hash")
        if type(block_cbor) is not bytes or not 1 <= len(block_cbor) <= MAX_BLOCK_BYTES:
            raise CardanoStoreError("Expected a bounded fetched Cardano block")
        try:
            decoded = decode_block_header(block_cbor)
        except CardanoProtocolError as error:
            raise CardanoStoreError("Fetched Cardano block is malformed") from error
        if asdict(decoded) != header:
            raise CardanoStoreError("Fetched block header differs from the supplied block facts")
        return point

    def _ensure_event(self, session, header):
        event_ref = "syn://event/" + header["block_hash"]
        knowledge = {"profile": "syn.cardano-block/1", "blockchainRef": BLOCKCHAIN_REF,
            "blockHash": header["block_hash"], "slot": header["slot"],
            "blockNumber": header["block_number"], "previousBlockHash": header["previous_hash"],
            "era": header["era"], "headerCborSha256": hashlib.sha256(header["cbor"]).hexdigest()}
        snapshot = {"id": event_ref, "name": "Cardano block " + header["block_hash"],
                    "typeRef": "syn://type/cardano-block", "knowledge": knowledge,
                    "occurredAt": None}
        db.insert_ignore(session, db.primitive["events"], {**snapshot,
            "knowledgeUpdated": datetime.now(UTC)}, ["id"])
        native = session.execute(select(db.primitive["events"]).where(
            db.primitive["events"].c.id == event_ref)).mappings().one()
        if any(native[key] != value for key, value in snapshot.items()):
            raise CardanoStoreConflict("Block hash Event already has different native contents")
        encoded = canonical(snapshot).decode("ascii")
        values = {"hash": header["block_hash"], "eventRef": event_ref,
            "eventWorkspace": event_workspace(event_ref),
            "slot": header["slot"], "blockNumber": header["block_number"],
            "previousHash": header["previous_hash"], "era": header["era"],
            "headerCbor": header["cbor"], "snapshot": encoded, "contentHash": _digest(snapshot)}
        db.insert_ignore(session, db.blocks, values, ["hash"])
        old = session.execute(select(db.blocks).where(db.blocks.c.hash == header["block_hash"])).mappings().one()
        if any(old[key] != value for key, value in values.items()):
            raise CardanoStoreConflict("Cardano block hash has conflicting retained header contents")
        db.insert_ignore(session, db.eventTargets, {"eventRef": event_ref,
            "targetRef": BLOCKCHAIN_REF, "role": "blockchain", "position": 0}, ["eventRef", "targetRef"])
        return snapshot

    def _observe(self, session, state, event, action, revision, at, extra):
        ember_ref = "syn://ember/cardano-observation-" + _digest({"source": state["id"],
            "revision": revision, "event": event["id"], "action": action})
        snapshot = {"id": ember_ref, "typeRef": "syn://type/cardano-block-observation",
            "name": "Cardano block " + ("observation" if action == "apply" else "retraction"),
            "knowledge": {"profile": "syn.cardano-observation/1", "action": action,
                "nodeRef": state["nodeRef"], "eventRef": event["id"], "peer": state["peer"],
                "networkMagic": state["networkMagic"], "revision": revision,
                "observedAt": at.isoformat(), **extra}}
        session.execute(db.primitive["embers"].insert().values(**snapshot, knowledgeUpdated=at))
        payload = {"profile": "syn.cardano-observation/1", "emberRef": ember_ref,
            "eventRef": event["id"], "contentHash": _digest(snapshot), "snapshot": snapshot,
            "eventSnapshot": event}
        message = {"message_id": str(uuid5(NAMESPACE_URL, ember_ref)), "channel": "node.cardano",
                   "sender": "node-" + _digest(state["nodeRef"]), "payload": payload}
        node_receipt = self.ledger.appendInTransaction(session, workspace_for(state["nodeRef"]), message)
        event_receipt = self.ledger.appendInTransaction(session, event_workspace(event["id"]), message)
        for reference, receipt in ((state["nodeRef"], node_receipt), (event["id"], event_receipt)):
            sequence = session.execute(select(func.max(db.chronicle.c.sequenceNumber)).where(
                db.chronicle.c.objectRef == reference, db.chronicle.c.kind == "ledger")).scalar() or 0
            session.execute(db.chronicle.insert().values(objectRef=reference, eventRef=event["id"],
                kind="ledger", begunAt=at, sequenceNumber=sequence + 1,
                details={"profile": "syn.cardano-ledger-entry/1", "emberRef": ember_ref,
                    "action": action, "contentHash": _digest(snapshot),
                    "workspace": receipt["workspace_id"], "receiptSequence": receipt["sequence"],
                    "receiptHash": receipt["hash"]}))
        session.execute(db.records.insert().values(emberRef=ember_ref, followerId=state["id"],
            eventRef=event["id"], revision=revision, action=action,
            snapshot=canonical(snapshot).decode("ascii"), contentHash=_digest(snapshot),
            nodeReceiptSequence=node_receipt["sequence"], nodeReceiptHash=node_receipt["hash"],
            eventReceiptSequence=event_receipt["sequence"], eventReceiptHash=event_receipt["hash"]))
        db.insert_ignore(session, db.eventTargets, {"eventRef": event["id"], "targetRef": ember_ref,
            "role": "observation-result", "position": 1}, ["eventRef", "targetRef"])
        return ember_ref

    def apply(self, node_ref, network_magic, peer, header, block_cbor, *, observed_at=None):
        point, at = self._header(header, block_cbor), _observed_at(observed_at)
        with self._transaction() as session:
            state = self._state(session, node_ref, network_magic, peer)
            if not state["initialized"]:
                raise CardanoStoreConflict("Source must intersect before applying blocks")
            if state["point"] == point:
                old = session.execute(select(db.chain).where(db.chain.c.followerId == state["id"],
                    db.chain.c.blockHash == point["hash"], db.chain.c.active.is_(True))).mappings().first()
                if old is not None:
                    self._ensure_event(session, header)
                    return {"action": "duplicate", "event_ref": "syn://event/" + point["hash"],
                            "ember_ref": old["observationRef"], "point": point, "revision": state["revision"]}
                raise CardanoStoreConflict("Source replayed an unobserved intersection header instead of its successor")
            if state["point"] is not None:
                if header["previous_hash"] != state["point"]["hash"] or point["slot"] <= state["point"]["slot"]:
                    raise CardanoStoreConflict("Roll-forward is not a successor of the durable source checkpoint")
                previous_number = session.execute(select(db.blocks.c.blockNumber).where(
                    db.blocks.c.hash == state["point"]["hash"])).scalar_one_or_none()
                if previous_number is not None and header["block_number"] != previous_number + 1:
                    raise CardanoStoreConflict("Roll-forward block number is not contiguous")
            event = self._ensure_event(session, header)
            self._lock_targets(session, node_ref, [event["id"]])
            revision = state["revision"] + 1
            ember_ref = self._observe(session, state, event, "apply", revision, at, {
                "blockCborSha256": hashlib.sha256(block_cbor).hexdigest(), "blockBytes": len(block_cbor)})
            db.insert_ignore(session, db.chain, {"followerId": state["id"], "blockHash": point["hash"],
                "slot": point["slot"], "blockNumber": header["block_number"], "active": True,
                "observationRef": ember_ref}, ["followerId", "blockHash"])
            session.execute(db.chain.update().where(db.chain.c.followerId == state["id"],
                db.chain.c.blockHash == point["hash"]).values(active=True, observationRef=ember_ref))
            self._checkpoint(session, state, point, "apply", at)
            session.execute(db.followers.update().where(db.followers.c.id == state["id"]).values(
                point=point, revision=revision, lastObservedAt=at))
            return {"action": "apply", "event_ref": event["id"], "ember_ref": ember_ref,
                    "point": point, "revision": revision}

    def rollback(self, node_ref, network_magic, peer, point, *, observed_at=None):
        point, at = _point(point), _observed_at(observed_at)
        with self._transaction() as session:
            return self._rollback(session, self._state(session, node_ref, network_magic, peer), point, at)

    def _rollback(self, session, state, point, at, action="rollback"):
        if not state["initialized"]:
            raise CardanoStoreConflict("Source must intersect before a rollback")
        if state["point"] == point:
            return {"action": "unchanged", "point": point, "revision": state["revision"], "retracted": 0}
        if point is not None and (state["point"] is None or point["slot"] > state["point"]["slot"]):
            raise CardanoStoreConflict("Rollback cannot advance the durable source checkpoint")
        active = select(db.chain).where(db.chain.c.followerId == state["id"], db.chain.c.active.is_(True))
        target = None
        if point is not None:
            target = session.execute(active.where(db.chain.c.blockHash == point["hash"],
                db.chain.c.slot == point["slot"])).mappings().first()
            first_slot = session.execute(select(func.min(db.chain.c.slot)).where(
                db.chain.c.followerId == state["id"], db.chain.c.active.is_(True))).scalar()
            if target is None and point != state["anchor"] and first_slot is not None and point["slot"] >= first_slot:
                raise CardanoStoreConflict("Rollback point conflicts with this source's retained chain")
        selection = active if target is None else active.where(db.chain.c.blockNumber > target["blockNumber"])
        affected = session.execute(selection.order_by(db.chain.c.blockNumber.desc())
                                   .limit(MAX_ROLLBACK_BLOCKS + 1)).mappings().all()
        if len(affected) > MAX_ROLLBACK_BLOCKS:
            raise CardanoStoreConflict("Rollback exceeds the bounded local recovery transaction")
        events = ["syn://event/" + row["blockHash"] for row in affected]
        self._lock_targets(session, state["nodeRef"], events)
        revision = state["revision"] + 1
        for row in affected:
            encoded = session.execute(select(db.blocks.c.snapshot).where(
                db.blocks.c.hash == row["blockHash"])).scalar_one()
            self._observe(session, state, json.loads(encoded), "rollback", revision, at,
                          {"retracts": row["observationRef"], "rollbackTo": point})
        if affected:
            session.execute(db.chain.update().where(db.chain.c.followerId == state["id"],
                db.chain.c.blockHash.in_([row["blockHash"] for row in affected])).values(active=False))
        self._checkpoint(session, state, point, action, at, len(affected))
        values = {"point": point, "revision": revision, "lastObservedAt": at}
        if target is None:
            values["anchor"] = point
        session.execute(db.followers.update().where(db.followers.c.id == state["id"]).values(**values))
        return {"action": action, "point": point, "revision": revision, "retracted": len(affected)}

    def status(self, node_ref, network_magic):
        local_ref(node_ref, "thing")
        with self.sessions() as session:
            rows = session.execute(select(db.followers).where(db.followers.c.nodeRef == node_ref,
                db.followers.c.networkMagic == network_magic).order_by(db.followers.c.peer)).mappings().all()
            peers = []
            for row in rows:
                count = session.execute(select(func.count()).select_from(db.chain).where(
                    db.chain.c.followerId == row["id"], db.chain.c.active.is_(True))).scalar_one()
                peers.append({"peer": row["peer"], "initialized": row["initialized"],
                    "point": row["point"], "revision": row["revision"], "active_blocks": count,
                    "last_observed_at": utc(row["lastObservedAt"]).isoformat() if row["lastObservedAt"] else None})
            return {"node_ref": node_ref, "network_magic": network_magic,
                    "blockchain_ref": BLOCKCHAIN_REF, "peers": peers}
