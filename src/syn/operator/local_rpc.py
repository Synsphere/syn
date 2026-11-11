"""Operator-owned local service routing. Application policy stays with the service."""
from __future__ import annotations

import hashlib
import os
import socket
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from syn_client.local_rpc import (
    DISPATCH_PROFILE,
    READY_PROFILE,
    RECEIPT_DOMAIN,
    REQUEST_PROFILE,
    SERVICE_REPLY_PROFILE,
    method_name,
    payload_size,
    receipt_body,
)
from syn_client.operator_protocol import (
    SynProtocolError,
    canonical,
    exact,
    parse,
    peer_uid,
    read_exact,
    receive,
    send,
    set_deadline,
    uuid,
)

from .custody import private_read

CONFIG_PROFILE = "syn.local-rpc-route/1"


@dataclass(frozen=True)
class LocalRpcRoute:
    path: Path
    sha256: str
    workspace_id: str
    method: str
    service: Path
    service_uid: int

    @classmethod
    def load(cls, path: Path) -> LocalRpcRoute:
        raw = private_read(path)
        data = exact(parse(raw), {"profile", "workspace_id", "method", "service_socket", "service_uid"},
                     "Syn RPC route policy")
        uid = data["service_uid"]
        if (data["profile"] != CONFIG_PROFILE or type(uid) is not int or not 0 < uid < 2**32 - 1
                or not isinstance(data["service_socket"], str) or "\0" in data["service_socket"]):
            raise SynProtocolError("Invalid Syn RPC route policy")
        service = Path(data["service_socket"])
        if not service.is_absolute() or str(service) != data["service_socket"] or len(os.fsencode(service)) > 103:
            raise SynProtocolError("Invalid Syn RPC service socket path")
        return cls(path, hashlib.sha256(raw).hexdigest(), uuid(data["workspace_id"]),
                   method_name(data["method"]), service, uid)

    def _service_entry(self):
        if self.service.resolve(strict=True) != self.service:
            raise SynProtocolError("Syn RPC service path changed or traverses a link")
        for parent in self.service.parents:
            info = parent.stat()
            sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if info.st_uid not in {0, self.service_uid, os.getuid()} or (info.st_mode & 0o022 and not sticky_root):
                raise SynProtocolError("Syn RPC service path has an untrusted ancestor")
        entry = self.service.lstat()
        if (not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != self.service_uid or entry.st_mode & 0o007
                or self.service.parent.stat().st_mode & 0o007):
            raise SynProtocolError("Syn RPC service endpoint has unsafe ownership or permissions")
        return entry

    def _require_live(self, node):
        if node.stopped.is_set() or hashlib.sha256(private_read(self.path)).hexdigest() != self.sha256:
            raise SynProtocolError("Syn RPC route changed or the node is stopping")

    def dispatch(self, conn, request, node):
        header = exact(request, {"profile", "workspace_id", "method", "request_id", "route_sha256", "size", "operator"},
                       "Syn RPC request")
        if (header["profile"] != REQUEST_PROFILE or header["route_sha256"] != self.sha256
                or header["workspace_id"] != self.workspace_id or header["method"] != self.method):
            raise SynProtocolError("Syn RPC request differs from the configured route")
        request_id = uuid(header["request_id"])
        size = payload_size(header["size"])
        self._require_live(node)
        deadline = time.monotonic() + 2
        status = node.status(header["operator"])
        send(conn, {"profile": READY_PROFILE, "operator": status}, deadline=deadline)
        payload = read_exact(conn, size, deadline=deadline)
        self._require_live(node)
        entry = self._service_entry()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as service:
            set_deadline(service, deadline)
            service.connect(str(self.service))
            if peer_uid(service) != self.service_uid or not os.path.samestat(entry, self._service_entry()):
                raise SynProtocolError("Syn RPC destination changed or has the wrong OS identity")
            send(service, {
                "profile": DISPATCH_PROFILE, "workspace_id": self.workspace_id, "method": self.method,
                "request_id": request_id, "size": size, "boot_id": node.boot_id, "caller_uid": peer_uid(conn),
            }, deadline=deadline)
            set_deadline(service, deadline)
            service.sendall(payload)
            reply = exact(receive(service, deadline=deadline), {"profile", "request_id", "size"}, "Syn RPC service reply")
            if reply["profile"] != SERVICE_REPLY_PROFILE or reply["request_id"] != request_id:
                raise SynProtocolError("Syn RPC service reply does not match the request")
            body = read_exact(service, payload_size(reply["size"]), deadline=deadline)
        self._require_live(node)
        proof = receipt_body(header, node.boot_id, payload, body)
        signature = node.signer.sign(RECEIPT_DOMAIN + canonical(proof)).hex()
        send(conn, {"body": proof, "signature": signature}, deadline=deadline)
        set_deadline(conn, deadline)
        conn.sendall(body)
