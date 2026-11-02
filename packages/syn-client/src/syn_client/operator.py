"""Public authenticated operator status. No key creation, storage or signing."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import socket
import stat
import sys
import time

from .custody import verify_ed25519
from .operator_protocol import SynProtocolError, canonical, exact, hex_bytes, peer_uid, receive, send
from .releases import RuntimeContract

CHALLENGE_PROFILE = "syn.operator-challenge/1"
STATUS_PROFILE = "syn.operator-status/1"
STATUS_DOMAIN = b"syn.operator-status/1\0"


def host_platform() -> str:
    os_name = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    architecture = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower())
    if os_name is None or architecture is None:
        raise SynProtocolError("Use the native Linux/macOS host; Windows deploys through WSL")
    return os_name + "-" + architecture


def verify_status(value, *, challenge: str, expected_hash: str, expected_version: str,
                  configuration_sha256: str, now: int | None = None):
    outer = exact(value, {"status", "signature"}, "operator status envelope")
    body = exact(outer["status"], {"profile", "challenge", "base_public_key", "mode", "runtime",
                 "configuration_sha256", "pid", "boot_id", "issued_at", "state_sha256", "boot",
                 "allocation_count", "custody", "network_id", "agent_activation_authorized"}, "operator status")
    public = hex_bytes(body["base_public_key"], 32, "operator public key")
    if (hashlib.sha256(public).hexdigest() != expected_hash or body["profile"] != STATUS_PROFILE
            or body["challenge"] != challenge or body["mode"] != "operator"
            or body["configuration_sha256"] != configuration_sha256
            or body["agent_activation_authorized"] is not False):
        raise SynProtocolError("Operator status differs from its independent identity/configuration pin")
    hex_bytes(challenge, 32, "fresh challenge")
    hex_bytes(body["state_sha256"], 32, "state digest")
    hex_bytes(body["boot_id"], 16, "boot identity")
    if (body["custody"] not in {"linux-secret-service", "macos-keychain"}
            or not isinstance(body["network_id"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", body["network_id"])):
        raise SynProtocolError("Invalid operator custody/network profile")
    for name in ("issued_at", "pid", "boot", "allocation_count"):
        if type(body[name]) is not int or not (0 if name == "allocation_count" else 1) <= body[name] < 2**63:
            raise SynProtocolError("Invalid bounded operator status integer")
    if body["allocation_count"] > 128:
        raise SynProtocolError("Operator allocation count exceeds the bounded profile")
    at = int(time.time()) if now is None else now
    if not at - 15 <= body["issued_at"] <= at + 5:
        raise SynProtocolError("Operator readiness is not fresh")
    runtime = RuntimeContract.from_dict(body["runtime"])
    if (runtime.runtime_version != expected_version or "operator" not in runtime.node_modes
            or "syn.operator-local/1" not in runtime.api_profiles):
        raise SynProtocolError("Operator runtime does not satisfy the installed release contract")
    verify_ed25519(public, hex_bytes(outer["signature"], 64, "status signature"), STATUS_DOMAIN + canonical(body))
    return body


def operator_status(*, socket_path: Path, expected_hash: str, expected_version: str,
                    configuration_sha256: str, owner_uid: int):
    if os.name != "posix" or type(owner_uid) is not int or owner_uid < 0:
        raise SynProtocolError("Operator IPC requires an explicit native host owner")
    hex_bytes(expected_hash, 32, "independently pinned operator identity")
    hex_bytes(configuration_sha256, 32, "configuration digest")
    if (not socket_path.is_absolute() or socket_path.resolve() != socket_path
            or socket_path.is_symlink() or len(os.fsencode(socket_path)) > 103):
        raise SynProtocolError("Operator socket must be canonical, private and bounded")
    parent, entry = socket_path.parent.stat(), socket_path.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != owner_uid or parent.st_mode & 0o077
            or not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != owner_uid or entry.st_mode & 0o077):
        raise SynProtocolError("Operator socket is not privately owned")
    nonce = secrets.token_hex(32)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(3)
        conn.connect(str(socket_path))
        if peer_uid(conn) != owner_uid:
            raise SynProtocolError("Operator process has the wrong OS identity")
        send(conn, {"profile": CHALLENGE_PROFILE, "challenge": nonce,
                    "expected_base_public_key_hash": expected_hash,
                    "configuration_sha256": configuration_sha256})
        value = receive(conn)
    verify_status(value, challenge=nonce, expected_hash=expected_hash, expected_version=expected_version,
                  configuration_sha256=configuration_sha256)
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--expected-key", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--configuration-sha256", required=True)
    parser.add_argument("--owner-uid", required=True, type=int)
    options = parser.parse_args(argv)
    try:
        value = operator_status(socket_path=options.socket, expected_hash=options.expected_key,
                                expected_version=options.expected_version,
                                configuration_sha256=options.configuration_sha256, owner_uid=options.owner_uid)
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception:
        print("Operator authentication/readiness failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
