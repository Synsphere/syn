"""Independent, public-only verification of a retained Agent node's readiness."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import secrets
import socket
import stat
import time

from .custody import verify_ed25519
from .operator_protocol import SynProtocolError, canonical, exact, hex_bytes, peer_uid, receive, send
from .releases import RuntimeContract

CHALLENGE_PROFILE = "syn.agent-challenge/1"
STATUS_PROFILE = "syn.agent-status/1"
STATUS_DOMAIN = b"syn.agent-status/1\0"


def verify_status(value, *, challenge, expected_hash, expected_operator, expected_version,
                  configuration_sha256, assignment_sha256, expected_group, expected_epoch, now=None):
    outer = exact(value, {"status", "signature"}, "Agent status envelope")
    body = exact(outer["status"], {"profile", "challenge", "mode", "selected_public_key", "operator_public_key_hash",
                 "runtime", "configuration_sha256", "assignment_sha256", "pid", "boot_id", "boot", "issued_at",
                 "state_sha256", "custody", "network_id", "device_profile", "hardware_attested", "cog_execution_authorized", "group_certificate"}, "Agent status")
    public = hex_bytes(body["selected_public_key"], 32, "Agent selected public key")
    if (expected_hash == expected_operator or body["profile"] != STATUS_PROFILE or body["mode"] != "agent" or body["challenge"] != challenge
            or hashlib.sha256(public).hexdigest() != expected_hash
            or body["operator_public_key_hash"] != expected_operator
            or body["configuration_sha256"] != configuration_sha256 or body["assignment_sha256"] != assignment_sha256
            or body["custody"] != "linux-secret-service" or body["device_profile"] != "syn.host-bound-virtual-device/1"
            or body["hardware_attested"] is not False or body["cog_execution_authorized"] is not False):
        raise SynProtocolError("Agent status differs from independently pinned assignment, profile or configuration")
    for name, value in (("fresh nonce", challenge), ("operator identity", expected_operator),
                        ("configuration", configuration_sha256), ("assignment", assignment_sha256),
                        ("state", body["state_sha256"])):
        hex_bytes(value, 32, name)
    if challenge == "00" * 32:
        raise SynProtocolError("Agent challenge must be fresh and nonzero")
    hex_bytes(body["boot_id"], 16, "Agent boot identity")
    for name in ("pid", "boot", "issued_at"):
        if type(body[name]) is not int or not 1 <= body[name] < 2**63:
            raise SynProtocolError("Agent status has an invalid bounded integer")
    at = int(time.time()) if now is None else now
    if not at - 15 <= body["issued_at"] <= at + 5:
        raise SynProtocolError("Agent readiness proof is stale")
    runtime = RuntimeContract.from_dict(body["runtime"])
    if (runtime.runtime_version != expected_version or "agent" not in runtime.node_modes
            or "syn.agent-local/1" not in runtime.api_profiles):
        raise SynProtocolError("Agent runtime differs from the selected release contract")
    verify_ed25519(public, hex_bytes(outer["signature"], 64, "Agent readiness signature"), STATUS_DOMAIN + canonical(body))
    from .group import verify_group
    group = verify_group(body["group_certificate"], expected_operator=expected_operator, expected_node=expected_hash,
                         expected_group=expected_group, expected_epoch=expected_epoch)
    if group["network_id"] != body["network_id"]:
        raise SynProtocolError("Agent readiness group belongs to another network")
    return body


def agent_status(*, socket_path: Path, owner_uid: int, expected_hash: str, expected_operator: str,
                 expected_version: str, configuration_sha256: str, assignment_sha256: str, expected_group: str, expected_epoch: int):
    if (os.name != "posix" or type(owner_uid) is not int or owner_uid < 1
            or not socket_path.is_absolute() or socket_path.resolve() != socket_path or socket_path.is_symlink()
            or len(os.fsencode(socket_path)) > 103):
        raise SynProtocolError("Agent IPC requires a canonical endpoint and restricted native owner")
    directory, entry = socket_path.parent.stat(), socket_path.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != owner_uid or directory.st_mode & 0o077
            or not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != owner_uid or entry.st_mode & 0o077):
        raise SynProtocolError("Agent IPC endpoint is not private to its assigned native account")
    nonce = secrets.token_hex(32)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(3)
        conn.connect(str(socket_path))
        if peer_uid(conn) != owner_uid:
            raise SynProtocolError("Agent endpoint is served by another native identity")
        send(conn, {"profile": CHALLENGE_PROFILE, "challenge": nonce,
                    "expected_public_key_hash": expected_hash, "configuration_sha256": configuration_sha256})
        value = receive(conn)
    verify_status(value, challenge=nonce, expected_hash=expected_hash, expected_operator=expected_operator,
                  expected_version=expected_version, configuration_sha256=configuration_sha256,
                  assignment_sha256=assignment_sha256, expected_group=expected_group, expected_epoch=expected_epoch)
    return value


def main(argv=None):
    import argparse
    import json
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--owner-uid", required=True, type=int)
    parser.add_argument("--expected-key", required=True)
    parser.add_argument("--expected-operator", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-group", required=True)
    parser.add_argument("--expected-epoch", type=int, required=True)
    parser.add_argument("--configuration-sha256", required=True)
    parser.add_argument("--assignment-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        value = agent_status(socket_path=args.socket, owner_uid=args.owner_uid, expected_hash=args.expected_key,
            expected_operator=args.expected_operator, expected_version=args.expected_version,
            configuration_sha256=args.configuration_sha256, assignment_sha256=args.assignment_sha256,
            expected_group=args.expected_group, expected_epoch=args.expected_epoch)
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception:
        print("Syn Agent readiness refused; check independently pinned assignment and private custody", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
