"""Public-only native pub-sub client. No Syn runtime imports or private keys."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import sys

from .operator_protocol import SynProtocolError, canonical, exact, parse, peer_uid, receive, send
from .pubsub import scope, verify_reply


def private_connect(path, owner_uid):
    if (os.name != "posix" or type(owner_uid) is not int or owner_uid <= 0
            or not path.is_absolute() or path.resolve() != path or path.is_symlink()
            or len(os.fsencode(path)) > 103):
        raise SynProtocolError("Pub-sub requires a canonical private native Agent endpoint")
    parent, entry = path.parent.stat(), path.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != owner_uid or parent.st_mode & 0o077
            or not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != owner_uid or entry.st_mode & 0o077):
        raise SynProtocolError("Pub-sub endpoint differs from its selected native owner")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(3)
        connection.connect(str(path))
        if peer_uid(connection) != owner_uid:
            raise SynProtocolError("Pub-sub endpoint is served by another native owner")
        after = path.lstat()
        if (entry.st_dev, entry.st_ino) != (after.st_dev, after.st_ino):
            raise SynProtocolError("Pub-sub endpoint changed during connection")
        return connection
    except BaseException:
        connection.close()
        raise


def request(*, socket_path, owner_uid, expected_node, configuration_sha256, selected_scope,
            operation, payload=None, expected_boot=None):
    selected = scope(selected_scope)
    if operation not in {"status", "publish", "receive"}:
        raise SynProtocolError("Unknown private pub-sub operation")
    value = {"profile": "syn.private-pubsub-request/1", "challenge": secrets.token_hex(32),
             "expected_public_key_hash": expected_node, "configuration_sha256": configuration_sha256,
             "operation": operation, "payload": payload}
    with private_connect(socket_path, owner_uid) as connection:
        send(connection, value)
        reply = receive(connection)
    verify_reply(reply, value, selected, expected_node=expected_node, expected_boot=expected_boot)
    return reply


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--owner-uid", required=True, type=int)
    parser.add_argument("--expected-key", required=True)
    parser.add_argument("--configuration-sha256", required=True)
    parser.add_argument("--expected-boot", required=True)
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.request.is_symlink() or not 0 < args.request.stat().st_size <= 65536:
            raise SynProtocolError("Public pub-sub request is linked or oversized")
        document = exact(parse(args.request.read_bytes()), {"scope", "operation", "payload"}, "public pub-sub request")
        result = request(socket_path=args.socket, owner_uid=args.owner_uid, expected_node=args.expected_key,
            configuration_sha256=args.configuration_sha256, selected_scope=document["scope"],
            operation=document["operation"], payload=document["payload"], expected_boot=args.expected_boot)
        sys.stdout.buffer.write(canonical(result) + b"\n")
        return 0
    except Exception:
        print(json.dumps({"code": "syn_pubsub_refused", "message": "Reconcile pinned Agent identity, live private mesh and custody"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
