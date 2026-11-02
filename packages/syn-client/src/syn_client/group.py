"""Public group-assignment verification; no group secret or signing API."""
from __future__ import annotations

import hashlib
import re
from uuid import UUID

from .custody import verify_ed25519
from .operator_protocol import SynProtocolError, canonical, exact, hex_bytes

CERT_DOMAIN = b"syn.agent.group-certificate.v1\0"
COMMIT_DOMAIN = b"syn.private-group.key-commitment.v1\0"


def group_id(value):
    try:
        valid = isinstance(value, str) and str(UUID(value)) == value and UUID(value).int != 0
    except ValueError:
        valid = False
    if not valid:
        raise SynProtocolError("Group identity must be a canonical nonzero UUID")
    return value


def group_epoch(value):
    if type(value) is not int or not 1 <= value < 2**31:
        raise SynProtocolError("Group epoch must be a bounded positive integer")
    return value


def verify_group(certificate, *, expected_operator, expected_node, expected_group, expected_epoch):
    group_id(expected_group)
    group_epoch(expected_epoch)
    hex_bytes(expected_operator, 32, "independent group issuer pin")
    hex_bytes(expected_node, 32, "independent assigned-node pin")
    packet = exact(certificate, {"body", "operator_public_key", "signature"}, "group certificate")
    body = exact(packet["body"], {"profile", "operator_public_key_hash", "node_public_key_hash", "network_id",
                 "group_id", "group_epoch", "group_key_commitment"}, "group assignment")
    pub = hex_bytes(packet["operator_public_key"], 32, "group issuer")
    group_id(body["group_id"])
    group_epoch(body["group_epoch"])
    hex_bytes(body["group_key_commitment"], 32, "group key commitment")
    if (expected_operator == expected_node or not isinstance(body["network_id"], str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", body["network_id"])
            or body["profile"] != "syn.agent-group-certificate/1" or hashlib.sha256(pub).hexdigest() != expected_operator
            or body["operator_public_key_hash"] != expected_operator or body["node_public_key_hash"] != expected_node
            or body["group_id"] != expected_group or body["group_epoch"] != expected_epoch):
        raise SynProtocolError("Group certificate differs from independent operator, node, group or epoch pins")
    verify_ed25519(pub, hex_bytes(packet["signature"], 64, "group certificate signature"), CERT_DOMAIN + canonical(body))
    return body
