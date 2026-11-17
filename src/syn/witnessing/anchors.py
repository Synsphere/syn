"""Independent public witness anchors in Windows DPAPI or native OS custody.

No reset/delete operation. Lost or rolled-back database state is not repaired
by adopting the remote's present answer. Recovery must preserve retained proof.
"""
from __future__ import annotations

import hashlib
import json

from ..operator.dag import canonical
from ..operator.dag_evidence import checkpoint_digest
from .protocol import classify, scoped_checkpoint


class CustodyAnchor:
    def __init__(self, server, peer):
        self.server, self.peer = server, peer

    @property
    def slot(self):
        return self.server.config["keyring_account"] + ".operator.witness-" + self.peer.operator + ".v1"

    def _host(self, action, checkpoint=None, proof=None):
        from ..operator.host_custody import request
        body = {"op": "dag_witness_anchor", "action": action,
            "service": self.server.config["keyring_service"], "account": self.server.config["keyring_account"],
            "pin": self.server.expected_hash, "peer": self.peer.operator, "network_id": self.peer.network}
        if action == "advance":
            body.update(checkpoint=checkpoint, proof=proof or [])
        return request(body)

    def read(self):
        from ..operator.host_custody import enabled
        if enabled():
            value = self._host("read")
        else:
            from ..operator.custody import _native_backend
            raw = _native_backend().get_password(self.server.config["keyring_service"], self.slot)
            value = json.loads(raw) if raw is not None else None
        if value is not None:
            scoped_checkpoint(value, self.peer)
        return value

    def advance(self, checkpoint, proof):
        from ..operator.host_custody import enabled
        body = scoped_checkpoint(checkpoint, self.peer)
        if enabled():
            result = self._host("advance", checkpoint, proof)
            if (not isinstance(result, dict) or result != {
                    "sequence": body["sequence"], "digest": checkpoint_digest(checkpoint)}):
                raise ValueError("Witness custody anchor acknowledgement differs")
            return
        from ..operator.custody import _agent_key_lock, _native_backend
        server = self.server
        # Separate from the lifetime Operator lock. PostgreSQL serializes the
        # sync job too; this lock also covers native callers in other processes.
        lock_name = "witness-" + hashlib.sha256(self.slot.encode()).hexdigest()[:24]
        with _agent_key_lock(server.directory, server.config["keyring_service"], lock_name):
            previous = self.read()
            bundle = None if previous is None else {"profile": "syn.operator-dag-consistency/1",
                "older": previous, "newer": checkpoint, "audit_path": proof}
            if classify(previous, checkpoint, self.peer, bundle) not in {"accepted", "unchanged"}:
                raise ValueError("Native witness anchor cannot rewind or fork")
            backend = _native_backend()
            encoded = canonical(checkpoint).decode("ascii")
            backend.set_password(server.config["keyring_service"], self.slot, encoded)
            if backend.get_password(server.config["keyring_service"], self.slot) != encoded:
                raise ValueError("Native witness anchor write/readback failed")


def reconcile(store, peer, anchor):
    """Retry the DB-commit/host-write gap without re-signing or trusting a new peer."""
    state = store.snapshot(peer)
    retained = anchor.read()
    accepted = state["accepted"]
    start = 0
    if retained is not None:
        scoped_checkpoint(retained, peer)
        matches = [i for i, row in enumerate(accepted) if row["checkpoint"] == retained]
        if len(matches) != 1:
            raise ValueError("Witness database is missing its independently retained custody checkpoint")
        start = matches[0] + 1
    for row in accepted[start:]:
        bundle = row["consistency"]
        anchor.advance(row["checkpoint"], bundle["audit_path"] if bundle else [])
    return state
