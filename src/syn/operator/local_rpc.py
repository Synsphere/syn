"""Operator-owned local service routing. Application policy stays with the service."""
from __future__ import annotations

import hashlib
import os
import socket
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from syn_client.local_rpc import (
    DISPATCH_PROFILE,
    READY_PROFILE,
    RECEIPT_DOMAIN,
    REQUEST_PROFILE,
    ROUTE_TABLE_PROFILE,
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
MAX_ROUTES = 32
_DESTINATION_FIELDS = {"workspace_id", "method", "service_socket", "service_uid"}


@dataclass(frozen=True)
class LocalRpcDestination:
    service: Path
    service_uid: int

    def socket_entry(self):
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


@dataclass(frozen=True)
class LocalRpcRoutes:
    path: Path
    sha256: str
    destinations: Mapping[tuple[str, str], LocalRpcDestination]

    @classmethod
    def load(cls, path: Path) -> LocalRpcRoutes:
        raw = private_read(path)
        data = parse(raw)
        profile = data.get("profile")
        if profile == CONFIG_PROFILE:
            exact(data, {"profile", *_DESTINATION_FIELDS}, "Syn RPC route policy")
            rows = [{name: data[name] for name in _DESTINATION_FIELDS}]
        elif profile == ROUTE_TABLE_PROFILE:
            exact(data, {"profile", "routes"}, "Syn RPC route table")
            rows = data["routes"]
            if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROUTES:
                raise SynProtocolError("Invalid Syn RPC route table size")
        else:
            raise SynProtocolError("Unknown Syn RPC route policy profile")

        destinations = {}
        for row in rows:
            route = exact(row, _DESTINATION_FIELDS, "Syn RPC destination")
            uid, address = route["service_uid"], route["service_socket"]
            if type(uid) is not int or not 0 < uid < 2**32 - 1 or not isinstance(address, str) or "\0" in address:
                raise SynProtocolError("Invalid Syn RPC destination")
            service = Path(address)
            if not service.is_absolute() or str(service) != address or len(os.fsencode(service)) > 103:
                raise SynProtocolError("Invalid Syn RPC service socket path")
            key = uuid(route["workspace_id"]), method_name(route["method"])
            if key in destinations:
                raise SynProtocolError("Duplicate Syn RPC workspace and method")
            destinations[key] = LocalRpcDestination(service, uid)
        return cls(path, hashlib.sha256(raw).hexdigest(), MappingProxyType(destinations))

    def _require_live(self, node):
        if node.stopped.is_set() or hashlib.sha256(private_read(self.path)).hexdigest() != self.sha256:
            raise SynProtocolError("Syn RPC route changed or the node is stopping")

    def dispatch(self, conn, request, node):
        header = exact(request, {"profile", "workspace_id", "method", "request_id", "route_sha256", "size", "operator"},
                       "Syn RPC request")
        if header["profile"] != REQUEST_PROFILE or header["route_sha256"] != self.sha256:
            raise SynProtocolError("Syn RPC request differs from the configured route")
        workspace = uuid(header["workspace_id"])
        method = method_name(header["method"])
        destination = self.destinations.get((workspace, method))
        if destination is None:
            raise SynProtocolError("Syn RPC workspace and method have no configured destination")
        request_id = uuid(header["request_id"])
        size = payload_size(header["size"])
        self._require_live(node)
        deadline = time.monotonic() + 2
        status = node.status(header["operator"])
        send(conn, {"profile": READY_PROFILE, "operator": status}, deadline=deadline)
        payload = read_exact(conn, size, deadline=deadline)
        self._require_live(node)
        entry = destination.socket_entry()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as service:
            set_deadline(service, deadline)
            service.connect(str(destination.service))
            if peer_uid(service) != destination.service_uid or not os.path.samestat(entry, destination.socket_entry()):
                raise SynProtocolError("Syn RPC destination changed or has the wrong OS identity")
            send(service, {
                "profile": DISPATCH_PROFILE, "workspace_id": workspace, "method": method,
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
