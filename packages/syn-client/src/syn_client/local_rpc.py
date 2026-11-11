"""Directed RPC through the private native Syn operator connection."""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .custody import verify_ed25519
from .operator import CHALLENGE_PROFILE, _operator_connection, verify_status
from .operator_protocol import (
    SynProtocolError,
    canonical,
    exact,
    hex_bytes,
    read_exact,
    receive,
    send,
    set_deadline,
    uuid,
)

RPC_PROFILE = "syn.operator-local-rpc/1"
ROUTE_TABLE_PROFILE = "syn.local-rpc-routes/1"
REQUEST_PROFILE = "syn.local-rpc-request/1"
READY_PROFILE = "syn.local-rpc-ready/1"
DISPATCH_PROFILE = "syn.local-rpc-dispatch/1"
SERVICE_REPLY_PROFILE = "syn.local-rpc-service-reply/1"
RECEIPT_PROFILE = "syn.local-rpc-receipt/1"
RECEIPT_DOMAIN = b"syn.local-rpc-receipt/1\0"
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


def payload_size(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_PAYLOAD_BYTES:
        raise SynProtocolError("Invalid Syn RPC payload length")
    return value


def method_name(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", value):
        raise SynProtocolError("Invalid Syn RPC method")
    return value


def receipt_body(header, boot_id: str, request: bytes, reply: bytes):
    return {
        "profile": RECEIPT_PROFILE,
        "boot_id": boot_id,
        "route_sha256": header["route_sha256"],
        "workspace_id": header["workspace_id"],
        "method": header["method"],
        "request_id": header["request_id"],
        "challenge": header["operator"]["challenge"],
        "request_sha256": hashlib.sha256(request).hexdigest(),
        "reply_sha256": hashlib.sha256(reply).hexdigest(),
        "size": len(reply),
    }


@dataclass(frozen=True)
class LocalRpcResult:
    payload: bytes
    receipt: dict


def call(
    payload: bytes, *, socket_path: Path, owner_uid: int, expected_hash: str,
    expected_version: str, configuration_sha256: str, route_sha256: str,
    workspace_id: str, method: str, request_id: str,
) -> LocalRpcResult:
    """Send one request via Syn. An interrupted exchange may have reached the service."""
    if type(payload) is not bytes:
        raise SynProtocolError("Syn RPC requires an opaque byte payload")
    for value in (expected_hash, configuration_sha256, route_sha256):
        hex_bytes(value, 32, "Syn RPC pin")
    nonce = secrets.token_hex(32)
    header = {
        "profile": REQUEST_PROFILE, "workspace_id": uuid(workspace_id), "method": method_name(method),
        "request_id": uuid(request_id), "route_sha256": route_sha256, "size": payload_size(len(payload)),
        "operator": {"profile": CHALLENGE_PROFILE, "challenge": nonce,
                     "expected_base_public_key_hash": expected_hash,
                     "configuration_sha256": configuration_sha256},
    }
    deadline = time.monotonic() + 5
    with _operator_connection(socket_path, owner_uid) as conn:
        send(conn, header, deadline=deadline)
        ready = exact(receive(conn, deadline=deadline), {"profile", "operator"}, "Syn RPC readiness")
        if ready["profile"] != READY_PROFILE:
            raise SynProtocolError("Unexpected Syn RPC readiness profile")
        status = verify_status(
            ready["operator"], challenge=nonce, expected_hash=expected_hash,
            expected_version=expected_version, configuration_sha256=configuration_sha256,
        )
        if RPC_PROFILE not in status["runtime"]["api_profiles"]:
            raise SynProtocolError("Operator release does not advertise directed local RPC")
        set_deadline(conn, deadline)
        conn.sendall(payload)
        receipt = exact(receive(conn, deadline=deadline), {"body", "signature"}, "Syn RPC receipt")
        body = receipt["body"]
        if not isinstance(body, dict):
            raise SynProtocolError("Invalid Syn RPC receipt body")
        reply = read_exact(conn, payload_size(body.get("size")), deadline=deadline)
    expected = receipt_body(header, status["boot_id"], payload, reply)
    if canonical(body) != canonical(expected):
        raise SynProtocolError("Syn RPC reply does not match the selected request")
    verify_ed25519(bytes.fromhex(status["base_public_key"]), hex_bytes(receipt["signature"], 64, "RPC signature"),
                   RECEIPT_DOMAIN + canonical(expected))
    return LocalRpcResult(reply, receipt)
