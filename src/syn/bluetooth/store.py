"""Atomic Bluetooth Event/Ember observations in the node and Event ledgers.

The received identity is a radio claim, never proof of key ownership. Native
targets are linked only when they already exist: PostgreSQL otherwise creates
placeholder objects for unresolved local references. No received advertisement
may create or update a purported peer Thing.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import math
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import func, select, text

from syn.positions.store import workspace_for
from syn.positions.types import local_ref, timestamp
from syn.recurrence import utc
from syn.refs import ObjectRef
from syn_client.protocol import GENESIS_HASH, canonical, validate_message, verify_receipt
from . import schema as db


KINDS = frozenset({"transmission-requested", "transmission-started",
    "transmission-stopped", "transmission-failed", "reception", "discovery", "loss"})
MAX_INPUT_BYTES = 49_152
PROFILE = "syn.bluetooth-observation/1"


class BluetoothStoreError(ValueError):
    """The observation is invalid or exceeds its bounded storage contract."""


class BluetoothStoreConflict(BluetoothStoreError):
    """A retry or native projection contradicts a retained observation."""


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def event_workspace(event_ref: str) -> str:
    return str(uuid5(NAMESPACE_URL, "syn-object-ledger:" + local_ref(event_ref, "event")))


def _json_payload(value):
    """Detach bounded JSON and normalize integral floats before JSONB stores it."""
    remaining = 4096

    def walk(item, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 12:
            raise BluetoothStoreError("Bluetooth payload is too deeply nested or complex")
        if item is None or type(item) is bool:
            return item
        if type(item) is str:
            if len(item) > 16_384:
                raise BluetoothStoreError("Bluetooth payload string exceeds 16384 characters")
            if "\x00" in item:
                raise BluetoothStoreError("Bluetooth JSON strings cannot contain NUL")
            try:
                item.encode("utf-8")
            except UnicodeEncodeError as error:
                raise BluetoothStoreError("Bluetooth JSON strings must be valid Unicode") from error
            return item
        if type(item) is int:
            if abs(item) > 9_007_199_254_740_991:
                raise BluetoothStoreError("Bluetooth payload integer exceeds portable JSON precision")
            return item
        if type(item) is float:
            if not math.isfinite(item) or abs(item) > 9_007_199_254_740_991:
                raise BluetoothStoreError("Bluetooth payload number must be finite and bounded")
            return int(item) if item.is_integer() else item
        if type(item) is list:
            if len(item) > 256:
                raise BluetoothStoreError("Bluetooth payload array exceeds 256 entries")
            return [walk(child, depth + 1) for child in item]
        if type(item) is dict:
            if len(item) > 256 or any(type(key) is not str or not 1 <= len(key) <= 128 for key in item):
                raise BluetoothStoreError("Bluetooth payload requires bounded string object keys")
            return {walk(key, depth + 1): walk(child, depth + 1) for key, child in item.items()}
        raise BluetoothStoreError("Bluetooth payload must contain JSON values, not bytes or objects")

    if type(value) is not dict:
        raise BluetoothStoreError("Bluetooth payload must be a JSON object")
    result = walk(value)
    try:
        encoded = canonical(result)
    except (UnicodeError, ValueError) as error:
        raise BluetoothStoreError("Bluetooth payload is not canonical JSON") from error
    if len(encoded) > MAX_INPUT_BYTES:
        raise BluetoothStoreError("Bluetooth payload exceeds 49152 canonical bytes")
    return json.loads(encoded)


def validate_payload(value):
    """Preflight and detach a radio report before placing it in the write queue.

    Rejected radio input can be counted as observation loss without starting a
    database operation or treating malformed advertisements as a ledger outage.
    """
    return _json_payload(value)


class BluetoothStore:
    def __init__(self, sessions, ledger=None):
        self.sessions = sessions
        if ledger is None:
            from syn.communication import MessageStore
            ledger = MessageStore(sessions)
        self.ledger = ledger

    @staticmethod
    def beacon_ref(node_ref):
        return "syn://thing/bluetooth-beacon-" + digest(local_ref(node_ref, "thing"))

    @contextmanager
    def _transaction(self):
        with self.sessions.begin() as session:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(text("SET LOCAL lock_timeout = '10s'"))
                session.execute(text("SET LOCAL statement_timeout = '30s'"))
            yield session

    def _node_lock(self, session, node_ref):
        row = session.execute(select(db.nodes).where(
            db.nodes.c.nodeRef == node_ref).with_for_update()).mappings().first()
        if row is None or row["ledgerWorkspace"] != workspace_for(node_ref):
            raise BluetoothStoreConflict("Bluetooth requires its prepared Syn node ledger")

    def _lock_targets(self, session, node_ref, event_ref=None):
        references = {node_ref, *([event_ref] if event_ref else [])}
        if session.get_bind().dialect.name == "postgresql":
            for reference in sorted(references):
                session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                    {"key": "syn.object-ledger:" + reference})
        from syn.communication import heads
        workspaces = {workspace_for(node_ref)}
        if event_ref:
            workspaces.add(event_workspace(event_ref))
        for workspace in sorted(workspaces):
            db.insert_ignore(session, heads, {
                "workspace_id": workspace, "sequence": 0, "hash": GENESIS_HASH,
            }, ["workspace_id"])
            session.execute(select(heads).where(
                heads.c.workspace_id == workspace).with_for_update()).one()

    def _beacon_snapshot(self, node_ref):
        return {"id": self.beacon_ref(node_ref), "name": "Syn node Bluetooth beacon",
            "typeRef": db.BEACON_TYPE,
            "knowledge": {"profile": "syn.bluetooth-node-beacon/1", "nodeRef": node_ref,
                "branding": "Syn", "radioIdentityAuthentication": "unverified"}}

    def prepare(self, node_ref):
        node_ref = local_ref(node_ref, "thing")
        snapshot = self._beacon_snapshot(node_ref)
        with self._transaction() as session:
            self._node_lock(session, node_ref)
            db.seed_types(session)
            db.insert_ignore(session, db.primitive["things"], {
                **snapshot, "knowledgeUpdated": datetime.now(UTC),
            }, ["id"])
            native = session.execute(select(db.primitive["things"]).where(
                db.primitive["things"].c.id == snapshot["id"])).mappings().one()
            if any(native[key] != value for key, value in snapshot.items()):
                raise BluetoothStoreConflict("Bluetooth beacon Thing has conflicting native contents")
            self._lock_targets(session, node_ref)
            receipt = self.ledger.appendInTransaction(session, workspace_for(node_ref), {
                "message_id": str(uuid5(NAMESPACE_URL, snapshot["id"])),
                "channel": "node.bluetooth", "sender": "node-" + digest(node_ref),
                "payload": {"profile": "syn.bluetooth-beacon-registration/1", "nodeRef": node_ref,
                    "objectRef": snapshot["id"], "contentHash": digest(snapshot), "snapshot": snapshot},
            })
            return {"nodeRef": node_ref, "beaconRef": snapshot["id"],
                "workspace": workspace_for(node_ref), "receipt": receipt}

    def _identity(self, session, node_ref, claim, *, hint=None, is_syn=False):
        result = {"claimedNodeRef": None, "peerRef": None,
            "identityStatus": "hint-only" if hint is not None else
                ("branded-unidentified" if is_syn else "unidentified"),
            "authentication": "unverified", "verified": False}
        if hint is not None:
            result["identityHint"] = hint
        if claim is None:
            return result
        try:
            reference = ObjectRef(claim)
            if reference.objectTable != "thing" or len(reference.slug) > 255:
                raise ValueError("Expected a canonical native Thing reference")
        except (TypeError, ValueError):
            result["identityStatus"] = "invalid-claim"
            return result
        result["claimedNodeRef"] = claim
        thing = session.execute(select(db.primitive["things"].c.id,
            db.primitive["things"].c.typeRef).where(db.primitive["things"].c.id == claim)).mappings().first()
        if thing is None:
            result["identityStatus"] = "claimed-unknown"
        else:
            result.update(peerRef=claim, peerTypeRef=thing["typeRef"],
                identityStatus="claimed-self" if claim == node_ref else "claimed-known")
        return result

    def resolve_peer(self, node_ref, claimed_node_ref):
        node_ref = local_ref(node_ref, "thing")
        with self.sessions() as session:
            return self._identity(session, node_ref, claimed_node_ref)

    @staticmethod
    def _message_id(ember_ref):
        return str(uuid5(NAMESPACE_URL, ember_ref))

    @staticmethod
    def _snapshot(native, kind):
        result = {key: native[key] for key in ("id", "name", "typeRef", "knowledge")}
        if kind == "event":
            result["occurredAt"] = utc(native["occurredAt"]).isoformat() if native["occurredAt"] else None
        return result

    def _receipt(self, session, workspace, message_id):
        from syn.communication import messages
        row = session.execute(select(messages).where(messages.c.workspace_id == workspace,
            messages.c.message_id == message_id)).mappings().first()
        if row is None:
            raise BluetoothStoreConflict("Bluetooth observation is missing a matching ledger receipt")
        receipt = {"schema_version": 1, **dict(row)}
        verify_receipt(receipt, workspace)
        return receipt

    def _recover(self, session, node_ref, event_ref, ember_ref, request_hash, at, *, duplicate):
        native = {}
        for kind, reference in (("event", event_ref), ("ember", ember_ref)):
            row = session.execute(select(db.primitive[kind + "s"]).where(
                db.primitive[kind + "s"].c.id == reference)).mappings().first()
            if row is None:
                raise BluetoothStoreConflict("Bluetooth operation has incomplete native objects")
            native[kind] = self._snapshot(row, kind)
        knowledge = native["ember"]["knowledge"]
        if (knowledge.get("requestHash") != request_hash or knowledge.get("nodeRef") != node_ref
                or (at is not None and knowledge.get("observedAt") != at.isoformat())):
            raise BluetoothStoreConflict("Bluetooth operation ID already has different contents")
        own = self._receipt(session, workspace_for(node_ref), self._message_id(ember_ref))
        event = self._receipt(session, event_workspace(event_ref), self._message_id(ember_ref))
        retained = own["payload"]
        if (retained != event["payload"] or retained.get("profile") != PROFILE
                or retained.get("eventRef") != event_ref or retained.get("emberRef") != ember_ref
                or retained.get("snapshot") != native["ember"] or retained.get("eventSnapshot") != native["event"]
                or retained.get("contentHash") != digest(native["ember"])
                or retained.get("eventContentHash") != digest(native["event"])):
            raise BluetoothStoreConflict("Bluetooth native objects differ from their retained ledger snapshots")
        targets = [dict(row) for row in session.execute(select(db.eventTargets).where(
            db.eventTargets.c.eventRef == event_ref).order_by(db.eventTargets.c.position)).mappings()]
        if targets != native["event"]["knowledge"]["targets"]:
            raise BluetoothStoreConflict("Bluetooth Event targets differ from the retained observation")
        for reference, receipt in ((node_ref, own), (event_ref, event)):
            rows = session.execute(select(db.chronicle).where(db.chronicle.c.objectRef == reference,
                db.chronicle.c.eventRef == event_ref, db.chronicle.c.kind == "ledger")).mappings().all()
            if len(rows) != 1 or rows[0]["details"] != self._chronicle_details(ember_ref, knowledge["kind"], retained["contentHash"], receipt):
                raise BluetoothStoreConflict("Bluetooth observation has inconsistent native Chronicle receipts")
        identity = knowledge["identity"]
        return {"eventRef": event_ref, "emberRef": ember_ref, "receipt": own,
            "eventReceipt": event, "peerRef": identity["peerRef"],
            "identityStatus": identity["identityStatus"], "identity": identity,
            "duplicate": duplicate, "observedAt": knowledge["observedAt"]}

    @staticmethod
    def _chronicle_details(ember_ref, kind, content_hash, receipt):
        return {"profile": "syn.bluetooth-ledger-entry/1", "emberRef": ember_ref,
            "kind": kind, "contentHash": content_hash, "workspace": receipt["workspace_id"],
            "receiptSequence": receipt["sequence"], "receiptHash": receipt["hash"]}

    def record(self, node_ref, kind, payload, *, observed_at=None, operation_id=None):
        node_ref = local_ref(node_ref, "thing")
        if type(kind) is not str or kind not in KINDS:
            raise BluetoothStoreError("Unknown Bluetooth observation kind")
        payload = validate_payload(payload)
        at = timestamp(observed_at) if observed_at is not None else None
        operation_id = str(uuid4()) if operation_id is None else operation_id
        if (type(operation_id) is not str or not 1 <= len(operation_id) <= 256
                or any(ord(char) < 33 or ord(char) > 126 for char in operation_id)):
            raise BluetoothStoreError("Bluetooth operation ID must be bounded printable text")
        token = digest({"nodeRef": node_ref, "operationId": operation_id})
        event_ref, ember_ref = "syn://event/bluetooth-" + token, "syn://ember/bluetooth-observation-" + token
        request_hash = digest({"kind": kind, "payload": payload})
        with self._transaction() as session:
            self._node_lock(session, node_ref)
            beacon_ref = self.beacon_ref(node_ref)
            if session.execute(select(db.primitive["things"].c.id).where(
                    db.primitive["things"].c.id == beacon_ref)).first() is None:
                raise BluetoothStoreConflict("Bluetooth beacon service was not prepared")
            self._lock_targets(session, node_ref, event_ref)
            if session.execute(select(db.primitive["events"].c.id).where(
                    db.primitive["events"].c.id == event_ref)).first() is not None:
                return self._recover(session, node_ref, event_ref, ember_ref, request_hash, at, duplicate=True)
            at = at or datetime.now(UTC)
            candidate = payload.get("identity", payload)
            candidate = candidate if type(candidate) is dict else {}
            identity = self._identity(session, node_ref, candidate.get("claimedNodeRef"),
                hint=candidate.get("identityHint"), is_syn=candidate.get("isSyn") is True)
            transmission = kind.startswith("transmission-")
            typ = db.TRANSMISSION_TYPE if transmission else (db.RECEPTION_TYPE if kind == "reception" else db.LIFECYCLE_TYPE)
            targets = [{"eventRef": event_ref, "targetRef": node_ref, "role": "observing-node", "position": 0},
                {"eventRef": event_ref, "targetRef": ember_ref, "role": "observation-result", "position": 1}]
            if transmission:
                targets.append({"eventRef": event_ref, "targetRef": beacon_ref,
                    "role": "local-beacon", "position": len(targets)})
            if identity["peerRef"] is not None and identity["peerRef"] != node_ref:
                targets.append({"eventRef": event_ref, "targetRef": identity["peerRef"],
                    "role": "advertised-peer", "position": len(targets)})
            event = {"id": event_ref, "name": "Bluetooth " + kind, "typeRef": typ,
                "occurredAt": at.isoformat(), "knowledge": {"profile": "syn.bluetooth-event/1",
                    "nodeRef": node_ref, "kind": kind, "operationId": operation_id,
                    "observationRef": ember_ref, "identity": identity, "targets": targets}}
            ember = {"id": ember_ref, "name": "Bluetooth " + kind + " observation", "typeRef": db.OBSERVATION_TYPE,
                "knowledge": {"profile": PROFILE, "nodeRef": node_ref, "eventRef": event_ref,
                    "kind": kind, "operationId": operation_id, "requestHash": request_hash,
                    "observedAt": at.isoformat(), "identity": identity,
                    "airReceptionVerified": False, "payload": payload}}
            message = validate_message({"message_id": self._message_id(ember_ref),
                "channel": "node.bluetooth", "sender": "node-" + digest(node_ref),
                "payload": {"profile": PROFILE, "eventRef": event_ref, "emberRef": ember_ref,
                    "contentHash": digest(ember), "eventContentHash": digest(event),
                    "snapshot": ember, "eventSnapshot": event}})
            session.execute(db.primitive["events"].insert().values(
                **{**event, "occurredAt": at}, knowledgeUpdated=at))
            session.execute(db.primitive["embers"].insert().values(**ember, knowledgeUpdated=at))
            session.execute(db.eventTargets.insert(), targets)
            own = self.ledger.appendInTransaction(session, workspace_for(node_ref), message)
            event_receipt = self.ledger.appendInTransaction(session, event_workspace(event_ref), message)
            for reference, receipt in ((node_ref, own), (event_ref, event_receipt)):
                sequence = session.execute(select(func.max(db.chronicle.c.sequenceNumber)).where(
                    db.chronicle.c.objectRef == reference, db.chronicle.c.kind == "ledger")).scalar() or 0
                session.execute(db.chronicle.insert().values(objectRef=reference, eventRef=event_ref,
                    kind="ledger", begunAt=at, sequenceNumber=sequence + 1,
                    details=self._chronicle_details(ember_ref, kind, digest(ember), receipt)))
            return self._recover(session, node_ref, event_ref, ember_ref, request_hash, at, duplicate=False)
