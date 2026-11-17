"""Atomic immutable clock observations, statistics, estimates and ledger receipts."""
from __future__ import annotations

from datetime import UTC, datetime
import json
from uuid import uuid4

from sqlalchemy import select

from syn.positions import schema as positions
from syn.positions.store import PositionStore, workspace_for
from syn.positions.types import canonical, digest, timestamp
from syn.recurrence import utc
from . import schema as db
from .config import ClockConfig
from .estimator import advance, initial, project
from .types import ESTIMATE_TYPE, STATS_TYPE, ClockReading, iso_ns, paired_time


class ClockStore:
    def __init__(self, sessions, *, ledger=None):
        self.sessions = sessions
        self.position = PositionStore(sessions, ledger=ledger)
        self.ledger = self.position.ledger

    def _receipt(self, session, node_ref, payload):
        return self.ledger.appendInTransaction(session, workspace_for(node_ref), {
            "message_id": str(uuid4()), "channel": "node.clock", "sender": "node-"+digest(node_ref),
            "payload": payload})

    def _record(self, session, node_ref, kind, typ, knowledge):
        ref = f"syn://{kind}/clock-{uuid4().hex}"
        snapshot = {"id": ref, "typeRef": typ, "name": "Node clock "+kind, "knowledge": knowledge}
        if kind == "event":
            snapshot["occurredAt"] = knowledge["observedAt"]
        values = {**snapshot, "knowledgeUpdated": datetime.now(UTC)}
        if kind == "event":
            values["occurredAt"] = timestamp(values["occurredAt"])
        session.execute(positions.primitive[kind+"s"].insert().values(**values))
        receipt = self._receipt(session, node_ref, {"profile": "syn.clock-object/1", "kind": kind,
            "objectRef": ref, "contentHash": digest(snapshot), "snapshot": snapshot})
        session.execute(db.records.insert().values(objectRef=ref, nodeRef=node_ref, kind=kind,
            snapshot=canonical(snapshot).decode("ascii"), contentHash=digest(snapshot),
            receiptSequence=receipt["sequence"], receiptHash=receipt["hash"]))
        if kind == "event":
            session.execute(positions.eventTargets.insert().values(eventRef=ref, targetRef=node_ref,
                role="clock-subject", position=0))
        return ref

    def _row(self, session, node_ref):
        # All position and clock writers share node -> ledger lock ordering.
        self.position._lock(session, node_ref)
        row = session.execute(select(db.clocks).where(db.clocks.c.nodeRef == node_ref)).mappings().first()
        if row is not None and digest(json.loads(row["state"])) != row["stateHash"]:
            raise ValueError("Clock estimator state digest differs")
        return row

    def _publish(self, session, node_ref, state, estimate, old, event_ref, wall):
        state_hash = digest(state)
        revision = old["revision"]+1 if old else 1
        knowledge = {**estimate, "nodeRef": node_ref, "sessionId": state["sessionId"],
            "observedAt": iso_ns(wall), "rawSystemNs": wall, "stateHash": state_hash,
            "observationEventRef": event_ref, "signConvention": "UTC-minus-raw-system-clock",
            "uncertaintyMethod": "source-error-plus-measured-jitter-and-holdover"}
        ember = self._record(session, node_ref, "ember", ESTIMATE_TYPE, knowledge)
        values = {"sessionId": state["sessionId"], "emberRef": ember, "state": canonical(state).decode("ascii"),
                  "stateHash": state_hash, "revision": revision}
        if old:
            session.execute(db.clocks.update().where(db.clocks.c.nodeRef == node_ref).values(**values))
        else:
            session.execute(db.clocks.insert().values(nodeRef=node_ref, **values))
        self._receipt(session, node_ref, {"profile": "syn.clock-canonical/1", "nodeRef": node_ref,
            "sessionId": state["sessionId"], "emberRef": ember, "revision": revision,
            "previousEmberRef": old["emberRef"] if old else None, "stateHash": state_hash,
            "observationEventRef": event_ref})
        return {"emberRef": ember, "eventRef": event_ref, "revision": revision, "estimate": knowledge}

    def begin(self, node_ref, session_id, config=None, *, wall=None, mono=None):
        config = config or ClockConfig()
        if wall is None or mono is None:
            wall, mono, _ = paired_time()
        with self.sessions.begin() as session:
            old = self._row(session, node_ref)
            if old and old["sessionId"] == session_id:
                return {"emberRef": old["emberRef"], "revision": old["revision"]}
            state = initial(session_id, wall, mono)
            estimate = project(state, mono, config)
            ref = self._record(session, node_ref, "event", "syn://type/clock-session-started",
                {"observedAt": iso_ns(wall), "nodeRef": node_ref, "sessionId": session_id,
                 "utcOffsetMs": 0.0, "precisionMs": None, "uncertaintyMs": None})
            return self._publish(session, node_ref, state, estimate, old, ref, wall)

    def observe(self, node_ref, session_id, reading: ClockReading, config=None):
        config = config or ClockConfig()
        with self.sessions.begin() as session:
            old = self._row(session, node_ref)
            if old is None or old["sessionId"] != session_id:
                return {"status": "obsolete-session"}
            state, estimate, stats, step = advance(json.loads(old["state"]), reading, config)
            ref = self._record(session, node_ref, "event", reading.to_dict()["typeRef"],
                {**reading.to_dict(), "sessionId": session_id, "nodeRef": node_ref})
            if step is not None:
                self._record(session, node_ref, "event", "syn://type/clock-discontinuity",
                    {"observedAt": iso_ns(reading.systemNs), "nodeRef": node_ref,
                     "sessionId": session_id, "stepMs": step, "epoch": state["epoch"]})
            self._record(session, node_ref, "ember", STATS_TYPE,
                {**stats, "sessionId": session_id, "nodeRef": node_ref, "observationEventRef": ref})
            return self._publish(session, node_ref, state, estimate, old, ref, reading.systemNs)

    def current(self, node_ref):
        with self.sessions() as session:
            row = session.execute(select(db.clocks).where(db.clocks.c.nodeRef == node_ref)).mappings().first()
            if row is None:
                return None
            raw = session.execute(select(db.records.c.snapshot).where(db.records.c.objectRef == row["emberRef"])).scalar_one()
            if digest(json.loads(row["state"])) != row["stateHash"]:
                raise ValueError("Clock estimator state digest differs")
            return {"nodeRef": node_ref, "sessionId": row["sessionId"], "emberRef": row["emberRef"],
                    "revision": row["revision"], "estimate": json.loads(raw)["knowledge"],
                    "sources": {k: {"kind": v["kind"], "failures": v["failures"],
                        "lastGoodNs": v["lastGoodNs"], **v["statistics"]} for k,v in json.loads(row["state"])["sources"].items()}}

    def verify(self, node_ref):
        from syn.communication import heads, messages
        from syn_client.protocol import GENESIS_HASH, verify_receipt
        with self.sessions.begin() as session:
            current = self._row(session, node_ref)
            workspace = workspace_for(node_ref)
            head = session.execute(select(heads).where(heads.c.workspace_id == workspace).with_for_update()).mappings().one()
            objects, latest, previous, sequence = {}, None, GENESIS_HASH, 0
            for row in session.execute(select(messages).where(messages.c.workspace_id == workspace).order_by(messages.c.sequence)).mappings():
                receipt = {"schema_version": 1, **dict(row)}
                verify_receipt(receipt, workspace)
                if receipt["previous_hash"] != previous or receipt["sequence"] != sequence+1:
                    raise ValueError("Clock ledger chain is discontinuous")
                previous, sequence = receipt["hash"], receipt["sequence"]
                payload = receipt["payload"]
                if payload.get("profile") == "syn.clock-object/1":
                    if digest(payload["snapshot"]) != payload["contentHash"]:
                        raise ValueError("Clock object digest differs")
                    objects[payload["objectRef"]] = (payload, receipt)
                elif payload.get("profile") == "syn.clock-canonical/1":
                    latest = payload
            if (previous, sequence) != (head["hash"], head["sequence"]):
                raise ValueError("Clock ledger head differs")
            retained = session.execute(select(db.records).where(db.records.c.nodeRef == node_ref)).mappings().all()
            if len(retained) != len(objects):
                raise ValueError("Clock object index differs")
            for record in retained:
                payload, receipt = objects[record["objectRef"]]
                snapshot = json.loads(record["snapshot"])
                if (canonical(snapshot) != canonical(payload["snapshot"]) or record["contentHash"] != digest(snapshot)
                        or record["receiptSequence"] != receipt["sequence"] or record["receiptHash"] != receipt["hash"]):
                    raise ValueError("Clock record receipt differs")
                table = positions.primitive[record["kind"]+"s"]
                native = session.execute(select(table).where(table.c.id == record["objectRef"])).mappings().one()
                if any(canonical(native[k]) != canonical(snapshot[k]) for k in ("id", "typeRef", "name", "knowledge")):
                    raise ValueError("Native clock object was altered")
                if record["kind"] == "event" and utc(native["occurredAt"]) != timestamp(snapshot["occurredAt"]):
                    raise ValueError("Native clock event timestamp was altered")
            if current is None:
                if latest is not None:
                    raise ValueError("Clock canonical row is missing")
            elif latest is None or any(current[k] != latest[k] for k in ("sessionId", "emberRef", "revision", "stateHash")):
                raise ValueError("Clock canonical pointer differs from ledger")
            return {"objects": len(objects), "sequence": sequence, "hash": previous, "nodeRef": node_ref}
