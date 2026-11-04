"""Operator-private public allocation/boot state, anchored in its OS keystore.

The native anchor fixes the canonical path and exact committed bytes. A sealed
pending transition makes an interrupted replacement recoverable without
inventing state. Rewinding both the native keystore and filesystem is outside
this local anchor's guarantee; lifecycle recovery remains a separate gate.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

from syn_client.operator_protocol import SynProtocolError, canonical, exact, hex_bytes, parse
from .custody import _native_backend, private_directory, private_read


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    sync_directory(path.parent)


class OperatorState:
    def __init__(self, directory: Path, service: str, account: str, base_hash: str):
        private_directory(directory)
        self.directory, self.service, self.account = directory, service, account
        self.base_hash = base_hash
        self.path = directory / "authority.json"
        self.slot = account + ".operator.control-state.v1"

    def _anchor(self):
        try:
            value = _native_backend().get_password(self.service, self.slot)
        except Exception as exc:
            raise SynProtocolError("Native operator state anchor is unavailable") from exc
        if value is None:
            raise SynProtocolError("Operator state anchor is absent; no automatic recreation")
        body = exact(parse(value.encode()), {"profile", "path", "base_hash", "current", "pending"}, "state anchor")
        if (body["profile"] != "syn.operator-state-anchor/1" or body["path"] != str(self.path)
                or body["base_hash"] != self.base_hash):
            raise SynProtocolError("Operator state anchor belongs to another path or identity")
        return body

    def _store_anchor(self, body):
        raw = canonical(body).decode("ascii")
        if len(raw) > 65536:
            raise SynProtocolError("Operator anchor capacity reached")
        try:
            backend = _native_backend()
            backend.set_password(self.service, self.slot, raw)
            if backend.get_password(self.service, self.slot) != raw:
                raise ValueError("readback")
        except Exception as exc:
            raise SynProtocolError("Native operator state write/readback failed") from exc

    def create(self) -> None:
        self._create({"profile": "syn.operator-state/1", "base_hash": self.base_hash,
                      "boot": 0, "socket": None, "reservations": []})

    def _create(self, body) -> None:
        self._validate(body)
        backend = _native_backend()
        if self.path.exists() or self.path.is_symlink() or backend.get_password(self.service, self.slot) is not None:
            raise SynProtocolError("Operator allocation state exists or is incomplete")
        # First state is sealed before publishing any allocation register.
        self._store_anchor({"profile": "syn.operator-state-anchor/1", "path": str(self.path),
                            "base_hash": self.base_hash, "current": None, "pending": body})
        self._replace(body)
        self._store_anchor({**self._anchor(), "current": self.digest(body), "pending": None})

    @staticmethod
    def digest(body) -> str:
        return hashlib.sha256(canonical(body)).hexdigest()

    def _replace(self, body) -> None:
        raw = canonical(body)
        if len(raw) > 48000:
            raise SynProtocolError("Operator allocation journal capacity reached")
        pending = self.directory / ("transition-" + uuid4().hex + ".json")
        write_new(pending, raw)
        os.replace(pending, self.path)
        sync_directory(self.directory)

    def read(self, *, recover: bool = False):
        anchor = self._anchor()
        try:
            current = parse(private_read(self.path))
        except FileNotFoundError:
            current = None
        if anchor["pending"] is not None:
            pending = anchor["pending"]
            if not recover:
                raise SynProtocolError("Operator state has an unfinished transition")
            observed = self.digest(current) if current is not None else None
            if observed not in {anchor["current"], self.digest(pending)}:
                raise SynProtocolError("Operator state differs from its sealed transition")
            self._validate(pending)
            self._replace(pending)
            self._store_anchor({**anchor, "current": self.digest(pending), "pending": None})
            current = pending
            anchor = self._anchor()
        self._validate(current)
        if self.digest(current) != anchor["current"]:
            raise SynProtocolError("Operator state is altered or rolled back")
        return current

    def _validate(self, body) -> None:
        body = exact(body, {"profile", "base_hash", "boot", "socket", "reservations"}, "operator state")
        if (body["profile"] != "syn.operator-state/1" or body["base_hash"] != self.base_hash
                or type(body["boot"]) is not int or not 0 <= body["boot"] < 2**63
                or type(body["reservations"]) is not list or len(body["reservations"]) > 128):
            raise SynProtocolError("Invalid operator state")
        if body["socket"] is not None:
            item = exact(body["socket"], {"device", "inode"}, "recorded socket")
            if any(type(x) is not int or x < 0 for x in item.values()):
                raise SynProtocolError("Invalid recorded operator socket")
        seen = {name: set() for name in ("inheritance", "device_commitment", "pledge_offer_sha256", "pulse_sha256")}
        for reservation in body["reservations"]:
            item = exact(reservation, {"pledge_offer_sha256", "pulse_sha256", "device_commitment", "inheritance",
                         "reserved_at", "candidates_sha256"}, "allocation reservation")
            for name in ("pledge_offer_sha256", "pulse_sha256", "device_commitment", "candidates_sha256"):
                hex_bytes(item[name], 32, name)
            if (type(item["inheritance"]) is not int or not 0 <= item["inheritance"] <= 0xffffff
                    or type(item["reserved_at"]) is not int or not 0 < item["reserved_at"] < 2**63):
                raise SynProtocolError("Invalid allocation reservation")
            for name, values in seen.items():
                if item[name] in values:
                    raise SynProtocolError("Duplicate protected allocation")
                values.add(item[name])

    def commit(self, body) -> None:
        current = self.read()
        self._validate(body)
        if body["boot"] < current["boot"]:
            raise SynProtocolError("Operator boot counter cannot rewind")
        if body["reservations"][:len(current["reservations"])] != current["reservations"]:
            raise SynProtocolError("Operator allocations are append-only")
        self._commit(body)

    def _commit(self, body) -> None:
        anchor = self._anchor()
        # Capacity is checked before either persistence side changes.
        if len(canonical(body)) > 48000:
            raise SynProtocolError("Operator allocation journal capacity reached")
        self._store_anchor({**anchor, "pending": body})
        self._replace(body)
        self._store_anchor({**anchor, "current": self.digest(body), "pending": None})
