"""Windows DPAPI independent checkpoint anchor; no private DAG records cross IPC.

Native Linux/macOS custody is intentionally unchanged in this milestone.
"""
from __future__ import annotations

from .dag_evidence import verify_checkpoint, checkpoint_digest
from .host_custody import enabled, request


def _call(server, action, checkpoint=None):
    payload = {"op": "dag_anchor", "service": server.config["keyring_service"],
               "account": server.config["keyring_account"], "pin": server.expected_hash,
               "action": action}
    if checkpoint is not None:
        payload["checkpoint"] = checkpoint
    return request(payload)


def read(server):
    if not enabled():
        return None
    checkpoint = _call(server, "read")
    if checkpoint is not None:
        body = verify_checkpoint(checkpoint)
        if body["operator"] != server.expected_hash or body["network_id"] != server.config["network_id"]:
            raise ValueError("Independent Windows DAG anchor belongs to another Operator or network")
    return checkpoint


def advance(server, checkpoint):
    if not enabled():
        return None
    body = verify_checkpoint(checkpoint)
    if body["operator"] != server.expected_hash or body["network_id"] != server.config["network_id"]:
        raise ValueError("Cannot anchor foreign Operator DAG history")
    result = _call(server, "advance", checkpoint)
    if (not isinstance(result, dict) or result.get("sequence") != body["sequence"]
            or result.get("digest") != checkpoint_digest(checkpoint)):
        raise ValueError("Windows DAG anchor acknowledgement is invalid")
    return result
