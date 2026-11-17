"""Atomic radio scan Event, capability Embers, canonical inventory and node ledger.

No radio-specific SQL tables: the canonical inventory pointer is a native Thing.
The positionNodes lock serializes radio/clock/position ledger appends for a node.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from syn.positions.schema import primitive, typeParents, eventTargets, insert_ignore
from syn.positions.store import PositionStore, workspace_for
from syn.recurrence import utc as stored_utc
from syn.positions.types import local_ref, timestamp
from .types import ProviderResult, canonical, digest

INVENTORY_TYPE = "syn://type/radio-inventory"
SCAN_TYPE = "syn://type/radio-discovery-event"
DEVICE_TYPE = "syn://type/radio-capability-ember"
INVENTORY_EMBER_TYPE = "syn://type/radio-inventory-ember"
RADIO_SUBJECT_TYPE = "syn://type/radio-interface"


class RadioStore:
    def __init__(self, sessions, *, ledger=None):
        self.sessions = sessions
        self.position = PositionStore(sessions, ledger=ledger)
        self.ledger = self.position.ledger

    @staticmethod
    def inventory_ref(node_ref):
        return "syn://thing/radio-inventory-" + digest(node_ref)[:48]

    def _types(self, session):
        for slug, parent, desc in (
            ("radio-inventory", "thing", "Canonical radio inventory state of a Syn node"),
            ("radio-interface", "device", "Physical or virtual RF interface with known evidence"),
            ("radio-capability-ember", "ember", "Immutable observed radio interface description"),
            ("radio-inventory-ember", "ember", "Immutable complete canonical node radio inventory"),
            ("radio-discovery-event", "event", "A bounded passive node radio scan"),
        ):
            ref = "syn://type/" + slug
            insert_ignore(session, primitive["types"], {"id": ref, "name": slug,
                "knowledge": {"definition": desc, "module": "syn-radio", "ontologyVersion": 1}}, ["id"])
            insert_ignore(session, typeParents, {"childRef": ref, "parentRef": "syn://type/" + parent},
                          ["childRef", "parentRef"])

    def _receipt(self, session, node_ref, payload):
        return self.ledger.appendInTransaction(session, workspace_for(node_ref), {
            "message_id": str(uuid4()), "channel": "node.radio", "sender": "node-"+digest(node_ref),
            "payload": payload,
        })

    def _primitive(self, session, node_ref, kind, typ, knowledge, observed):
        ref = f"syn://{kind}/radio-{uuid4().hex}"
        snapshot = {"id": ref, "name": "Syn radio " + kind, "typeRef": typ, "knowledge": knowledge}
        if kind == "event":
            snapshot["occurredAt"] = timestamp(observed).isoformat()
        row = {**snapshot, "knowledgeUpdated": datetime.now(UTC)}
        if kind == "event":
            row["occurredAt"] = timestamp(observed)
        session.execute(primitive[kind+"s"].insert().values(**row))
        self._receipt(session, node_ref, {"profile": "syn.radio-object/1", "kind": kind,
                       "objectRef": ref, "contentHash": digest(snapshot), "snapshot": snapshot})
        if kind == "event":
            session.execute(eventTargets.insert().values(eventRef=ref, targetRef=node_ref,
                                                        role="radio-subject", position=0))
        return ref

    def record(self, node_ref: str, results: tuple[ProviderResult, ...], *, now=None):
        local_ref(node_ref, "thing")
        now = timestamp(now or datetime.now(UTC))
        if not isinstance(results, tuple) or any(not isinstance(p, ProviderResult) for p in results):
            raise TypeError("Radio scan must consist of provider results")
        if len({p.name for p in results}) != len(results):
            raise ValueError("Duplicate radio provider report")
        with self.sessions.begin() as session:
            # A position-aware node must have been bootstrapped first.
            self.position._lock(session, node_ref)
            self._types(session)
            ref = self.inventory_ref(node_ref)
            old = session.execute(select(primitive["things"]).where(
                primitive["things"].c.id == ref).with_for_update()).mappings().first()
            previous = dict(old["knowledge"]) if old is not None else {}
            devices = dict(previous.get("devices", {}))
            device_refs = dict(previous.get("deviceRefs", {}))
            report = [p.to_dict() for p in results]
            discovered = set()
            for p in results:
                if p.status != "complete":
                    continue
                source_ids = set()
                for device in p.devices:
                    descriptor = device.to_dict()
                    devices[device.id] = descriptor
                    source_ids.add(device.id)
                removed = {key for key, item in devices.items() if item["provider"] == p.name} - source_ids
                for key in removed:
                    devices.pop(key)
                    device_refs.pop(key, None)
                discovered |= source_ids
            if len(devices) > 512:
                raise ValueError("Radio inventory exceeds safety limit")
            ordered = {k: devices[k] for k in sorted(devices)}
            inventory_hash = digest(ordered)
            observed = now.isoformat()
            event = self._primitive(session, node_ref, "event", SCAN_TYPE, {
                "observedAt": observed, "nodeRef": node_ref, "providers": report,
                "discoveredDeviceIds": sorted(discovered), "inventoryHash": inventory_hash,
                "passive": True}, now)
            for key in sorted(ordered):
                original = previous.get("devices", {}).get(key)
                if original is None or canonical(original) != canonical(ordered[key]) or key not in device_refs:
                    device_refs[key] = self._primitive(session, node_ref, "ember", DEVICE_TYPE, {
                        "observedAt": observed, "nodeRef": node_ref,
                        "scanEventRef": event, "device": ordered[key]}, now)
            changed = old is None or inventory_hash != previous.get("inventoryHash")
            revision = int(previous.get("revision", 0)) + int(changed)
            inventory_ember = previous.get("inventoryEmberRef")
            if changed:
                inventory_ember = self._primitive(session, node_ref, "ember", INVENTORY_EMBER_TYPE, {
                    "observedAt": observed, "nodeRef": node_ref, "scanEventRef": event,
                    "inventoryHash": inventory_hash, "deviceRefsHash": digest(device_refs),
                    "deviceCount": len(ordered)}, now)
            state = {"nodeRef": node_ref, "devices": ordered, "deviceRefs": device_refs,
                     "inventoryHash": inventory_hash, "inventoryEmberRef": inventory_ember,
                     "revision": revision, "scanEventRef": event, "scannedAt": observed,
                     "providers": report}
            if old is None:
                session.execute(primitive["things"].insert().values(
                    id=ref, name="Syn node radio inventory", typeRef=INVENTORY_TYPE,
                    knowledge=state, knowledgeUpdated=now))
            else:
                session.execute(primitive["things"].update().where(primitive["things"].c.id == ref)
                                .values(knowledge=state, knowledgeUpdated=now))
            self._receipt(session, node_ref, {"profile": "syn.radio-canonical/1",
                "nodeRef": node_ref, "inventoryRef": ref,
                "revision": revision, "inventoryHash": inventory_hash,
                "inventoryEmberRef": inventory_ember,
                "scanEventRef": event, "scannedAt": observed, "changed": changed})
            return {"eventRef": event, "inventoryEmberRef": inventory_ember,
                    "revision": revision, "changed": changed, "count": len(ordered),
                    "providers": report}

    def current(self, node_ref: str):
        with self.sessions() as session:
            row = session.execute(select(primitive["things"].c.knowledge).where(
                primitive["things"].c.id == self.inventory_ref(node_ref))).scalar_one_or_none()
            if row is None:
                return None
            value = dict(row)
            if digest(value["devices"]) != value["inventoryHash"]:
                raise ValueError("Radio inventory differs from stored digest")
            return value

    def verify(self, node_ref: str):
        """Check whole ledger chain and radio snapshot/pointer correspondence."""
        from syn.communication import heads, messages
        from syn_client.protocol import GENESIS_HASH, verify_receipt
        with self.sessions.begin() as session:
            self.position._lock(session, node_ref)
            workspace = workspace_for(node_ref)
            head = session.execute(select(heads).where(heads.c.workspace_id == workspace)
                                   .with_for_update()).mappings().one()
            previous, number = GENESIS_HASH, 0
            latest, objects = None, {}
            for row in session.execute(select(messages).where(messages.c.workspace_id == workspace)
                                       .order_by(messages.c.sequence)).mappings():
                receipt = {"schema_version": 1, **dict(row)}
                verify_receipt(receipt, workspace)
                if receipt["previous_hash"] != previous or receipt["sequence"] != number + 1:
                    raise ValueError("Radio ledger discontinuity")
                previous, number = receipt["hash"], receipt["sequence"]
                payload = receipt["payload"]
                if payload.get("profile") == "syn.radio-object/1":
                    if digest(payload["snapshot"]) != payload["contentHash"]:
                        raise ValueError("Radio object digest mismatch")
                    objects[payload["objectRef"]] = payload["snapshot"]
                elif payload.get("profile") == "syn.radio-canonical/1":
                    latest = payload
            if (previous, number) != (head["hash"], head["sequence"]):
                raise ValueError("Radio ledger head mismatch")
            state = session.execute(select(primitive["things"].c.knowledge).where(
                primitive["things"].c.id == self.inventory_ref(node_ref))).scalar_one_or_none()
            if state is None or latest is None:
                raise ValueError("Radio canonical state missing")
            for key in ("revision", "inventoryHash", "inventoryEmberRef", "scanEventRef", "scannedAt"):
                if state[key] != latest[key]:
                    raise ValueError("Radio canonical pointer differs from ledger")
            if digest(state["devices"]) != state["inventoryHash"]:
                raise ValueError("Radio inventory digest mismatch")
            if state["inventoryEmberRef"] not in objects or state["scanEventRef"] not in objects:
                raise ValueError("Missing native radio object receipt")
            inventory_snapshot = objects[state["inventoryEmberRef"]]["knowledge"]
            if inventory_snapshot.get("inventoryHash") != digest(state["devices"]):
                raise ValueError("Radio inventory Ember differs from canonical devices")
            if inventory_snapshot.get("deviceRefsHash") != digest(state["deviceRefs"]):
                raise ValueError("Radio inventory Ember differs from canonical references")
            if inventory_snapshot.get("deviceCount") != len(state["devices"]):
                raise ValueError("Radio inventory Ember count differs")
            for device_id, ref in state["deviceRefs"].items():
                if ref not in objects or objects[ref]["knowledge"].get("device") != state["devices"][device_id]:
                    raise ValueError("Radio device Ember differs from canonical descriptor")
            for ref, snapshot in objects.items():
                kind = ref.split("/", 3)[2]
                table = primitive[kind+"s"]
                native = session.execute(select(table).where(table.c.id == ref)).mappings().one()
                for k in ("id", "typeRef", "name", "knowledge"):
                    if canonical(native[k]) != canonical(snapshot[k]):
                        raise ValueError("Native radio primitive altered")
                if kind == "event" and stored_utc(native["occurredAt"]) != timestamp(snapshot["occurredAt"]):
                    raise ValueError("Native radio event time altered")
            return {"nodeRef": node_ref, "sequence": number, "hash": previous,
                    "objects": len(objects), "revision": state["revision"]}
