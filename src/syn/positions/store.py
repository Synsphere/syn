"""Atomic native Event/Ember creation, canonical selection and existing ledger.

Every operation locks the node row BEFORE the communication head. This lock
order is shared by bootstrap, observation, custom-space updates and verification.
A ledger failure rolls back the native objects and canonical pointer as well.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
from uuid import NAMESPACE_URL, uuid4, uuid5
from sqlalchemy import select, text

from syn.recurrence import utc as stored_utc
from . import schema as db
from .types import (Estimate, Reading, Space, WGS84, GEOGRAPHIC, LEGACY_GEOGRAPHIC,
                    canonical, choose_estimate,
                    digest, local_ref, timestamp)


def workspace_for(nodeRef: str) -> str:
    return str(uuid5(NAMESPACE_URL, "syn-node-ledger:"+local_ref(nodeRef, "thing")))


class PositionStore:
    def __init__(self, sessions, *, ledger=None):
        self.sessions = sessions
        if ledger is None:
            from syn.communication import MessageStore
            ledger = MessageStore(sessions)
        self.ledger = ledger

    def _lock(self, session, nodeRef):
        return session.execute(select(db.nodes).where(db.nodes.c.nodeRef == nodeRef)
                               .with_for_update()).mappings().one()

    def _ledger(self, session, nodeRef, payload, messageId=None):
        return self.ledger.appendInTransaction(session, workspace_for(nodeRef), {
            "message_id": messageId or str(uuid4()), "channel": "node.position",
            # sender protocol is limited to 128 ASCII characters, ObjectRefs can be longer.
            "sender": "node-"+digest(nodeRef), "payload": payload})

    def _record(self, session, nodeRef, kind, typeRef, knowledge, *, sampleKey=None):
        reference = f"syn://{kind}/position-{uuid4().hex}"
        snapshot = {"id": reference, "typeRef": typeRef, "name": f"Node position {kind}",
                    "knowledge": knowledge}
        if kind == "event":
            snapshot["occurredAt"] = knowledge["observedAt"]
        values = {**snapshot, "knowledgeUpdated": datetime.now(UTC)}
        if kind == "event":
            values["occurredAt"] = timestamp(values["occurredAt"])
        session.execute(db.primitive[kind+"s"].insert().values(**values))
        receipt = self._ledger(session, nodeRef, {"profile": "syn.position-object/1", "kind": kind,
                      "objectRef": reference, "contentHash": digest(snapshot), "snapshot": snapshot})
        session.execute(db.records.insert().values(objectRef=reference, nodeRef=nodeRef, kind=kind,
            snapshot=canonical(snapshot).decode("ascii"), contentHash=digest(snapshot),
            receiptSequence=receipt["sequence"], receiptHash=receipt["hash"], sampleKey=sampleKey))
        if kind == "event":
            session.execute(db.eventTargets.insert().values(eventRef=reference, targetRef=nodeRef,
                                                           role="position-subject", position=0))
        return reference

    def register_space(self, space: Space, *, session=None):
        if session is None:
            with self.sessions.begin() as session:
                return self.register_space(space, session=session)
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('syn.position.spaces',0))"))
        old = session.execute(select(db.spaces).where(db.spaces.c.spaceRef == space.reference)).mappings().first()
        if old is not None:
            if canonical(old["definition"]) != canonical(space.to_dict()):
                raise ValueError("A coordinate frame cannot change in place; create a new space and type")
            return
        existing_thing = session.execute(select(db.primitive["things"]).where(
            db.primitive["things"].c.id == space.reference)).mappings().first()
        if existing_thing is not None:
            raise ValueError("A new coordinate space needs an unused Thing reference")
        # The Type belongs to this frame. Existing unrelated Types cannot be hijacked.
        old_type = session.execute(select(db.primitive["types"]).where(
            db.primitive["types"].c.id == space.typeRef)).mappings().first()
        if old_type is not None and space != WGS84:
            raise ValueError("A custom space needs an unused concrete Type reference")
        db.insert_ignore(session, db.primitive["types"], {"id": space.typeRef, "name": space.typeRef.rsplit("/",1)[-1],
            "knowledge": {"coordinateSpace": space.to_dict(), "ontology": "syn-position"}}, ["id"])
        db.insert_ignore(session, db.typeParents, {"childRef": space.typeRef,
            "parentRef": "syn://type/space-position-estimate"}, ["childRef", "parentRef"])
        db.insert_ignore(session, db.primitive["things"], {"id": space.reference, "typeRef": "syn://type/coordinate-space",
            "name": space.reference.rsplit("/",1)[-1], "knowledge": space.to_dict()}, ["id"])
        for axis in space.axes:
            db.insert_ignore(session, db.typeProperties, {"type": space.typeRef, "name": axis,
                "propertyTypeRef": "syn://type/position-number", "min": 0, "max": 1}, ["type", "name"])
        session.execute(db.spaces.insert().values(spaceRef=space.reference, typeRef=space.typeRef,
                                                  definition=space.to_dict()))

    def bootstrap(self, nodeRef: str, prior: Estimate) -> list[dict]:
        local_ref(nodeRef, "thing")
        prior.validate_space(WGS84)
        with self.sessions.begin() as session:
            # Migration owns Type installation. Missing migrations are a hard error.
            self.register_space(WGS84, session=session)
            db.insert_ignore(session, db.primitive["things"], {"id": nodeRef, "typeRef": "syn://type/device",
                "name": "Syn node", "knowledge": {"positionLedgerWorkspace": workspace_for(nodeRef)}}, ["id"])
            db.insert_ignore(session, db.nodes, {"nodeRef": nodeRef, "ledgerWorkspace": workspace_for(nodeRef),
                "createdAt": datetime.now(UTC), "probeReport": []}, ["nodeRef"])
            self._lock(session, nodeRef)
            old = session.execute(select(db.positions).where(db.positions.c.nodeRef == nodeRef,
                                    db.positions.c.spaceRef == WGS84.reference)).mappings().first()
            if old is None:
                self._update(session, nodeRef, prior, None, None)
            else:
                self._upgrade_geographic_type(session, nodeRef)
        return self.current(nodeRef)

    def _current(self, session, nodeRef, spaceRef):
        return session.execute(select(db.positions).where(db.positions.c.nodeRef == nodeRef,
                         db.positions.c.spaceRef == spaceRef)).mappings().first()

    def _estimate(self, session, reference):
        raw = session.execute(select(db.records.c.snapshot).where(db.records.c.objectRef == reference)).scalar_one()
        return Estimate.from_dict(json.loads(raw)["knowledge"])

    def _space(self, session, ref):
        value = session.execute(select(db.spaces.c.definition).where(db.spaces.c.spaceRef == ref)).scalar_one()
        value = dict(value)
        value["axes"], value["units"] = tuple(value["axes"]), tuple(value["units"])
        return Space(**value)

    def upgrade_geographic_type(self, nodeRef: str) -> str | None:
        """Append a correctly typed replacement; never rewrite hashed history.

        Run Alembic revision 0017 first. This is safe to repeat and uses the same
        per-node lock, object ledger and canonical-update transaction as a fix.
        """
        with self.sessions.begin() as session:
            self._lock(session, nodeRef)
            return self._upgrade_geographic_type(session, nodeRef)

    def _upgrade_geographic_type(self, session, nodeRef):
        old = self._current(session, nodeRef, WGS84.reference)
        if old is None:
            return None
        estimate = self._estimate(session, old["emberRef"])
        if estimate.typeRef != LEGACY_GEOGRAPHIC:
            return None
        corrected = replace(estimate, typeRef=GEOGRAPHIC, provenance={
            **estimate.to_dict()["provenance"],
            "typeRename": {"previousTypeRef": LEGACY_GEOGRAPHIC,
                           "previousEmberRef": old["emberRef"],
                           "reason": "Terminology correction only; coordinates are unchanged"},
        })
        # Refuse a partial deployment: the immutable frame's metadata is changed
        # only by the dedicated, transactional Alembic migration.
        corrected.validate_space(self._space(session, WGS84.reference))
        return self._update(session, nodeRef, corrected, old, old["observationEventRef"],
                            lastObservedAt=stored_utc(old["lastObservedAt"]))

    def _update(self, session, nodeRef, estimate, old, eventRef, *, lastObservedAt=None):
        latest = estimate.observedAt if lastObservedAt is None else timestamp(lastObservedAt)
        knowledge = {**estimate.to_dict(), "nodeRef": nodeRef, "observationEventRef": eventRef}
        reference = self._record(session, nodeRef, "ember", estimate.typeRef, knowledge)
        values = {"emberRef": reference, "observationEventRef": eventRef,
                  "lastObservedAt": latest, "revision": (old["revision"]+1) if old else 1}
        if old:
            session.execute(db.positions.update().where(db.positions.c.nodeRef == nodeRef,
                db.positions.c.spaceRef == estimate.spaceRef).values(**values))
        else:
            session.execute(db.positions.insert().values(nodeRef=nodeRef, spaceRef=estimate.spaceRef, **values))
        self._ledger(session, nodeRef, {"profile": "syn.position-canonical/1", "nodeRef": nodeRef,
            "spaceRef": estimate.spaceRef, "previousEmberRef": old["emberRef"] if old else None,
            "emberRef": reference, "observationEventRef": eventRef, "revision": values["revision"],
            "lastObservedAt": latest.isoformat()})
        return reference

    def observe(self, nodeRef: str, reading: Reading, *, now=None, **policy) -> dict:
        with self.sessions.begin() as session:
            self._lock(session, nodeRef)
            existing = session.execute(select(db.records.c.objectRef).where(db.records.c.nodeRef == nodeRef,
                db.records.c.kind == "event", db.records.c.sampleKey == reading.sampleKey)).scalar_one_or_none()
            if existing:
                return {"decision": "duplicate", "eventRef": existing, "emberRef": None}
            eventRef = self._record(session, nodeRef, "event", reading.typeRef,
                                   {**reading.to_dict(), "nodeRef": nodeRef}, sampleKey=reading.sampleKey)
            if reading.estimate is None:
                return {"decision": "no-fix", "eventRef": eventRef, "emberRef": None}
            return self._reconcile(session, nodeRef, reading.estimate, eventRef, now or reading.receivedAt, **policy)

    def put(self, nodeRef: str, estimate: Estimate, *, now=None, **policy) -> dict:
        """Local trusted producer API for arbitrary registered spaces."""
        with self.sessions.begin() as session:
            self._lock(session, nodeRef)
            return self._reconcile(session, nodeRef, estimate, None, now or datetime.now(UTC), **policy)

    def _reconcile(self, session, nodeRef, estimate, eventRef, now, **policy):
        estimate.validate_space(self._space(session, estimate.spaceRef))
        if estimate.spaceRef == WGS84.reference:
            self._upgrade_geographic_type(session, nodeRef)
        old = self._current(session, nodeRef, estimate.spaceRef)
        oldEstimate = self._estimate(session, old["emberRef"]) if old else None
        decision = choose_estimate(oldEstimate, estimate, now=now,
                                  lastObservedAt=stored_utc(old["lastObservedAt"]) if old else None, **policy)
        reference = None
        if decision == "replace":
            reference = self._update(session, nodeRef, estimate, old, eventRef)
        elif decision == "unchanged" and estimate.observedAt > stored_utc(old["lastObservedAt"]):
            session.execute(db.positions.update().where(db.positions.c.nodeRef == nodeRef,
                db.positions.c.spaceRef == estimate.spaceRef).values(lastObservedAt=estimate.observedAt))
            self._ledger(session, nodeRef, {"profile": "syn.position-confirmation/1", "spaceRef": estimate.spaceRef,
                "emberRef": old["emberRef"], "eventRef": eventRef, "lastObservedAt": estimate.observedAt.isoformat()})
        return {"decision": decision, "eventRef": eventRef, "emberRef": reference}

    def current(self, nodeRef: str) -> list[dict]:
        with self.sessions() as session:
            values = session.execute(select(db.positions).where(db.positions.c.nodeRef == nodeRef)
                                     .order_by(db.positions.c.spaceRef)).mappings().all()
            return [{"spaceRef": row["spaceRef"], "emberRef": row["emberRef"], "revision": row["revision"],
                     "lastObservedAt": stored_utc(row["lastObservedAt"]).isoformat(),
                     "estimate": self._estimate(session, row["emberRef"]).to_dict()} for row in values]

    def verify(self, nodeRef: str) -> dict:
        """Validate stored native objects against ledger receipts and the head.

        This detects alteration relative to retained receipts, not wholesale DB
        rollback by a privileged adversary. Keep an external trusted head pin.
        """
        from syn.communication import messages, heads
        from syn_client.protocol import GENESIS_HASH, verify_receipt
        with self.sessions.begin() as session:
            self._lock(session, nodeRef)
            workspace = workspace_for(nodeRef)
            head = session.execute(select(heads).where(heads.c.workspace_id == workspace)
                                   .with_for_update()).mappings().one()
            chain = session.execute(select(messages).where(messages.c.workspace_id == workspace)
                                    .order_by(messages.c.sequence)).mappings()
            previous, seq, objects, selected, confirmed = GENESIS_HASH, 0, {}, {}, {}
            for row in chain:
                receipt = {"schema_version": 1, **dict(row)}
                verify_receipt(receipt, workspace)
                if receipt["previous_hash"] != previous or receipt["sequence"] != seq+1:
                    raise ValueError("Position ledger chain is discontinuous")
                previous, seq = receipt["hash"], receipt["sequence"]
                p = receipt["payload"]
                if p.get("profile") == "syn.position-object/1":
                    if digest(p["snapshot"]) != p["contentHash"]:
                        raise ValueError("Changed position object digest")
                    objects[p["objectRef"]] = (p, receipt)
                elif p.get("profile") == "syn.position-canonical/1":
                    selected[p["spaceRef"]] = p
                    confirmed[p["spaceRef"]] = p["lastObservedAt"]
                elif p.get("profile") == "syn.position-confirmation/1":
                    confirmed[p["spaceRef"]] = p["lastObservedAt"]
            if (seq, previous) != (head["sequence"], head["hash"]):
                raise ValueError("Position ledger head differs")
            retained = session.execute(select(db.records).where(db.records.c.nodeRef == nodeRef)).mappings().all()
            if len(retained) != len(objects):
                raise ValueError("Position object index differs from ledger")
            for record in retained:
                item, receipt = objects[record["objectRef"]]
                snapshot = json.loads(record["snapshot"])
                if (canonical(snapshot) != canonical(item["snapshot"]) or record["contentHash"] != item["contentHash"]
                        or record["receiptHash"] != receipt["hash"] or record["receiptSequence"] != receipt["sequence"]):
                    raise ValueError("Position snapshot differs from receipt")
                table = db.primitive[record["kind"]+"s"]
                native = session.execute(select(table).where(table.c.id == record["objectRef"])).mappings().one()
                for key in ("id", "typeRef", "name", "knowledge"):
                    if canonical(native[key]) != canonical(snapshot[key]):
                        raise ValueError("Native position primitive was altered")
                if record["kind"] == "event" and stored_utc(native["occurredAt"]) != timestamp(snapshot["occurredAt"]):
                    raise ValueError("Native position event timestamp was altered")
            current = session.execute(select(db.positions).where(db.positions.c.nodeRef == nodeRef)).mappings().all()
            if {r["spaceRef"] for r in current} != set(selected):
                raise ValueError("Canonical list differs from ledger")
            for row in current:
                expected = selected[row["spaceRef"]]
                if any(row[k] != expected[k] for k in ("emberRef", "revision", "observationEventRef")):
                    raise ValueError("Canonical pointer differs from ledger")
                if stored_utc(row["lastObservedAt"]).isoformat() != confirmed[row["spaceRef"]]:
                    raise ValueError("Canonical observation timestamp differs")
            return {"nodeRef": nodeRef, "ledgerWorkspace": workspace, "sequence": seq,
                    "hash": previous, "objects": len(retained), "spaces": len(current)}
