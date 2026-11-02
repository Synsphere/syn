"""Real native Syn operator process with protected identity/state and local IPC."""
from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import stat
import threading
import time

from syn.release import runtimeContract
from syn_client.operator import CHALLENGE_PROFILE, STATUS_DOMAIN, STATUS_PROFILE
from syn_client.operator_protocol import SynProtocolError, canonical, exact, hex_bytes, parse, peer_uid, receive, send

from .custody import _agent_key_lock, _entry, _native_backend, backend_profile, private_directory, private_read
from .hd_keys import PythonHdOperatorSigner, create_operator_identity
from .state import OperatorState, sync_directory, write_new

CONFIG_PROFILE = "syn.operator-config/1"


def load_config(path: Path):
    raw = private_read(path)
    body = exact(parse(raw), {"profile", "state_dir", "socket", "keyring_service", "keyring_account",
                              "expected_base_public_key_hash", "network_id"}, "operator configuration")
    if body["profile"] != CONFIG_PROFILE or body["state_dir"] != str(path.parent):
        raise SynProtocolError("Operator configuration has the wrong profile or state directory")
    if body["socket"] != str(path.parent / "operator.sock") or len(os.fsencode(body["socket"])) > 103:
        raise SynProtocolError("Operator socket path is not the fixed private endpoint")
    _entry(body["keyring_service"], body["keyring_account"])
    hex_bytes(body["expected_base_public_key_hash"], 32, "operator installation identity")
    if not isinstance(body["network_id"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", body["network_id"]):
        raise SynProtocolError("Invalid explicit operator network")
    return body, hashlib.sha256(raw).hexdigest()


def initialize(directory: Path, *, service: str, account: str, network_id: str):
    _entry(service, account)
    private_directory(directory)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", network_id):
        raise SynProtocolError("Invalid operator network")
    if len(os.fsencode(directory / "operator.sock")) > 103:
        raise SynProtocolError("Operator state path exceeds native socket limits")
    with _agent_key_lock(directory, service, account + ".provision"):
        if any(directory.iterdir()):
            raise SynProtocolError("Operator directory is not empty; no automatic identity replacement")
        backend = _native_backend()
        if backend.get_password(service, account + ".operator.control-state.v1") is not None:
            raise SynProtocolError("Operator allocation anchor already exists")
        # The retained marker fences an interrupted first installation.
        write_new(directory / "initializing.json", canonical({"profile": "syn.operator-initializing/1"}))
        public = create_operator_identity(service=service, account=account, lock_dir=directory)
        base_hash = str(public["base_public_key_hash"])
        state = OperatorState(directory, service, account, base_hash)
        state.create()
        config = {"profile": CONFIG_PROFILE, "state_dir": str(directory),
                  "socket": str(directory / "operator.sock"), "keyring_service": service,
                  "keyring_account": account, "expected_base_public_key_hash": base_hash,
                  "network_id": network_id}
        write_new(directory / "operator.json", canonical(config))
        return {"profile": "syn.operator-created/1", "identity": public,
                "configuration_sha256": hashlib.sha256(canonical(config)).hexdigest(),
                "custody": backend_profile(), "agent_activation_authorized": False}


class OperatorServer:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config, self.config_sha = load_config(config_path)
        self.directory = config_path.parent
        self.path = Path(self.config["socket"])
        self.expected_hash = self.config["expected_base_public_key_hash"]
        self.signer = PythonHdOperatorSigner(self.config["keyring_service"], self.config["keyring_account"], bytes.fromhex(self.expected_hash))
        self.state = OperatorState(self.directory, self.config["keyring_service"], self.config["keyring_account"], self.expected_hash)
        self.boot_id = secrets.token_hex(16)
        self.stopped = threading.Event()
        self.listener = None
        self.owned_inode = None

    def _clear_stale_socket(self, state):
        if not self.path.exists() and not self.path.is_symlink():
            if state["socket"] is not None:
                self.state.commit({**state, "socket": None})
            return
        entry = self.path.lstat()
        expected = {"device": entry.st_dev, "inode": entry.st_ino}
        if (not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_mode & 0o077
                or state["socket"] != expected):
            raise SynProtocolError("Existing socket is not the retained operator endpoint")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            try:
                probe.connect(str(self.path))
            except OSError as exc:
                if exc.errno != errno.ECONNREFUSED:
                    raise SynProtocolError("Existing operator socket has an ambiguous owner") from exc
            else:
                raise SynProtocolError("An operator endpoint is already serving")
        if self.path.lstat() != entry:
            raise SynProtocolError("Operator socket changed during recovery")
        self.path.unlink()
        sync_directory(self.directory)
        self.state.commit({**state, "socket": None})

    def status(self, request):
        request = exact(request, {"profile", "challenge", "expected_base_public_key_hash", "configuration_sha256"}, "operator challenge")
        nonce = hex_bytes(request["challenge"], 32, "operator challenge")
        if (nonce == bytes(32) or request["profile"] != CHALLENGE_PROFILE
                or request["expected_base_public_key_hash"] != self.expected_hash
                or request["configuration_sha256"] != self.config_sha):
            raise SynProtocolError("Operator challenge is not bound to this installation")
        if load_config(self.config_path)[1] != self.config_sha:
            raise SynProtocolError("Operator configuration changed while serving")
        current = self.state.read()
        body = {"profile": STATUS_PROFILE, "challenge": nonce.hex(), "mode": "operator",
                "base_public_key": self.signer.public_key.hex(), "runtime": runtimeContract().to_dict(),
                "configuration_sha256": self.config_sha, "pid": os.getpid(), "boot_id": self.boot_id,
                "issued_at": int(time.time()), "state_sha256": self.state.digest(current),
                "boot": current["boot"], "allocation_count": len(current["reservations"]),
                "custody": backend_profile(), "network_id": self.config["network_id"],
                "agent_activation_authorized": False}
        return {"status": body, "signature": self.signer.sign(STATUS_DOMAIN + canonical(body)).hex()}

    def health_request(self):
        return {"profile": CHALLENGE_PROFILE, "challenge": secrets.token_hex(32),
                "expected_base_public_key_hash": self.expected_hash, "configuration_sha256": self.config_sha}

    def caller_allowed(self, uid):
        return uid == os.getuid()

    def dispatch(self, conn, request):
        send(conn, self.status(request))

    def close_resources(self):
        pass

    def serve(self):
        with _agent_key_lock(self.directory, self.config["keyring_service"], self.config["keyring_account"] + ".operator"):
            current = self.state.read(recover=True)
            self._clear_stale_socket(current)
            current = self.state.read()
            self.state.commit({**current, "boot": current["boot"] + 1})
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener = listener
            previous = os.umask(0o077)
            try:
                listener.bind(str(self.path))
                os.chmod(self.path, 0o600)
                entry = self.path.lstat()
                self.owned_inode = {"device": entry.st_dev, "inode": entry.st_ino}
                self.state.commit({**self.state.read(), "socket": self.owned_inode})
                listener.listen(16)
                listener.settimeout(0.25)
                last_health = 0.0
                while not self.stopped.is_set():
                    if time.monotonic() - last_health > 2:
                        # A missing/locked key, altered config or state fences readiness
                        # and terminates this process; supervisors never regenerate it.
                        self.status(self.health_request())
                        last_health = time.monotonic()
                    try:
                        conn, _ = listener.accept()
                    except TimeoutError:
                        continue
                    with conn:
                        conn.settimeout(2)
                        try:
                            if not self.caller_allowed(peer_uid(conn)):
                                raise SynProtocolError("Operator caller has the wrong native user")
                            self.dispatch(conn, receive(conn))
                        except (SynProtocolError, OSError, ValueError):
                            # No diagnostic oracle or arbitrary signer is offered over IPC.
                            continue
            finally:
                os.umask(previous)
                self.close_resources()
                listener.close()
                if self.owned_inode is not None:
                    try:
                        entry = self.path.lstat()
                        if (stat.S_ISSOCK(entry.st_mode)
                                and {"device": entry.st_dev, "inode": entry.st_ino} == self.owned_inode):
                            self.path.unlink()
                            sync_directory(self.directory)
                    except FileNotFoundError:
                        pass
                    current = self.state.read()
                    self.state.commit({**current, "socket": None})


def run(config_path: Path):
    server = OperatorServer(config_path)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: server.stopped.set())
    server.serve()
