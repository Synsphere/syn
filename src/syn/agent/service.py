"""A real native Agent-mode process, using only its adopted leaf identity.

The private socket supplies readiness and scoped encrypted pub-sub. Neither
proof nor message authorizes Cog execution; Potbot retains that admission gate.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import secrets
import signal
import threading
import time

from syn.operator.custody import _agent_key_lock, backend_profile, private_read
from syn.operator.hd_keys import _pycardano
from syn.operator.service import OperatorServer
from syn.operator.state import OperatorState, write_new
from syn.release import runtimeContract
from syn_client.adoption import verify_adoption
from syn_client.custody import verify_ed25519
from syn_client.operator_protocol import SynProtocolError, canonical, exact, hex_bytes, parse
from syn_client.agent import CHALLENGE_PROFILE, STATUS_DOMAIN, STATUS_PROFILE
from .adoption import ADOPTED_SLOTS, _read_keyring
from syn.operator.custody import _native_backend
from .device import PROFILE, device_id, load_config


def activate(config_path, receipt):
    config, config_sha = load_config(config_path)
    directory = config_path.parent
    with _agent_key_lock(directory, config["keyring_service"], config["keyring_account"] + ".adoption"):
        # Verify historical assignment at its acceptance time; live readiness is
        # proved separately with a new nonce by the retained selected key.
        verify_adoption(receipt, expected_master_hash=bytes.fromhex(config["expected_operator"]),
                        now=receipt["receipt"]["adopted_at"])
        selected_hash = receipt["receipt"]["selected_public_key_hash"]
        signer = AgentSigner(config, selected_hash)
        from .group import load_group
        load_group(config, selected_hash)
        if signer.public_key.hex() != receipt["key_proof"]["proof"]["selected_public_key"]:
            raise SynProtocolError("Retained Agent leaf differs from assignment receipt")
        marker = {"profile": "syn.agent-assignment/1", "configuration_sha256": config_sha, "adoption": receipt}
        # Create the completion file first. Any interruption before the native
        # anchor leaves an incomplete installation; it cannot silently activate.
        write_new(directory / "assignment.json", canonical(marker))
        AgentState(directory, config["keyring_service"], config["keyring_account"], selected_hash).create()
        return {"profile": "syn.agent-assigned/1", "selected_public_key_hash": selected_hash,
                "custody": backend_profile(), "cog_execution_authorized": False}


class AgentState(OperatorState):
    def __init__(self, directory, service, account, selected_hash):
        super().__init__(directory, service, account, selected_hash)
        self.path = directory / "agent-state.json"
        self.slot = account + ".agent.control-state.v1"


class AgentSigner:
    def __init__(self, config, expected_hash):
        self.config = config
        self.expected_hash = expected_hash
        hex_bytes(expected_hash, 32, "assigned identity")

    def _values(self):
        config = self.config
        device_id(config)  # refuses cloned directory, owner or different host
        store = _native_backend()
        values = {name: _read_keyring(store, config["keyring_service"], config["keyring_account"], name)
                  for name in ADOPTED_SLOTS}
        if any(value is None for value in values.values()):
            raise SynProtocolError("Agent key custody is absent or incomplete")
        manifest = exact(parse(values["manifest"].encode("ascii")), {"schema_version", "type_ref", "grant_sha256",
                         "selected_public_key_hash", "master_public_key_hash", "offer_sha256", "inheritance", "independence"}, "Agent key marker")
        if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
                or manifest["type_ref"] != "synny://type/syn-agent-adopted-keystore-v1"
                or manifest["selected_public_key_hash"] != self.expected_hash
                or manifest["master_public_key_hash"] != config["expected_operator"]):
            raise SynProtocolError("Agent native key marker differs from independently pinned assignment")
        decoded = {label: hex_bytes(values[label], 64 if label == "xprivate" else 32, "Agent retained key")
                   for label in ("xprivate", "public", "chain")}
        if hashlib.sha256(decoded["public"]).hexdigest() != self.expected_hash:
            raise SynProtocolError("Agent retained public key changed")
        return decoded

    @property
    def public_key(self):
        return self._values()["public"]

    def sign(self, payload):
        if type(payload) is not bytes or not 0 < len(payload) <= 65536:
            raise SynProtocolError("Agent signer requires a bounded typed preimage")
        values = self._values()
        _, extended_cls = _pycardano()
        signature = extended_cls(values["xprivate"] + values["public"] + values["chain"]).sign(payload)
        verify_ed25519(values["public"], signature, payload)
        return signature


class AgentServer(OperatorServer):
    def __init__(self, config_path):
        self.config_path = config_path
        self.config, self.config_sha = load_config(config_path)
        self.directory = config_path.parent
        self.path = Path(self.config["socket"])
        assignment = exact(parse(private_read(self.directory / "assignment.json")),
                           {"profile", "configuration_sha256", "adoption"}, "Agent assignment")
        if assignment["profile"] != "syn.agent-assignment/1" or assignment["configuration_sha256"] != self.config_sha:
            raise SynProtocolError("Agent assignment belongs to another immutable configuration")
        receipt = assignment["adoption"]
        verify_adoption(receipt, expected_master_hash=bytes.fromhex(self.config["expected_operator"]),
                        now=receipt["receipt"]["adopted_at"])
        self.expected_hash = receipt["receipt"]["selected_public_key_hash"]
        self.assignment_sha = hashlib.sha256(canonical(assignment)).hexdigest()
        self.signer = AgentSigner(self.config, self.expected_hash)
        self.state = AgentState(self.directory, self.config["keyring_service"], self.config["keyring_account"], self.expected_hash)
        self.boot_id = secrets.token_hex(16)
        self.stopped = threading.Event()
        self.listener = None
        self.owned_inode = None
        from .pubsub import NativePubSub
        self.pubsub = NativePubSub(self)

    def dispatch(self, conn, request):
        from syn_client.operator_protocol import peer_uid, send
        if request.get("profile") == "syn.private-pubsub-link/1":
            self.status(self.health_request())
            self.pubsub.attach(conn, request)
        elif request.get("profile") == "syn.private-pubsub-request/1":
            if peer_uid(conn) != 0:
                raise SynProtocolError("Pub-sub control requires the privileged deployment controller")
            send(conn, self.pubsub.control(request))
        else:
            super().dispatch(conn, request)

    def close_resources(self):
        self.pubsub.close()

    def health_request(self):
        return {"profile": CHALLENGE_PROFILE, "challenge": secrets.token_hex(32),
                "expected_public_key_hash": self.expected_hash, "configuration_sha256": self.config_sha}

    def caller_allowed(self, uid):
        # Root may independently qualify another restricted native account.
        return uid in {0, os.getuid()}

    def status(self, request):
        if self.pubsub.fault.is_set():
            raise SynProtocolError("Private Syn transport failed; the Agent is fenced")
        exact(request, {"profile", "challenge", "expected_public_key_hash", "configuration_sha256"}, "Agent challenge")
        nonce = hex_bytes(request["challenge"], 32, "Agent nonce")
        if (nonce == bytes(32) or request["profile"] != CHALLENGE_PROFILE
                or request["expected_public_key_hash"] != self.expected_hash
                or request["configuration_sha256"] != self.config_sha
                or load_config(self.config_path)[1] != self.config_sha):
            raise SynProtocolError("Agent challenge or configuration pin differs")
        assignment = private_read(self.directory / "assignment.json")
        if hashlib.sha256(assignment).hexdigest() != self.assignment_sha:
            raise SynProtocolError("Agent assignment changed while running")
        current = self.state.read()
        from .group import load_group
        group = load_group(self.config, self.expected_hash)
        body = {"profile": STATUS_PROFILE, "challenge": nonce.hex(), "mode": "agent",
                "selected_public_key": self.signer.public_key.hex(), "operator_public_key_hash": self.config["expected_operator"],
                "runtime": runtimeContract().to_dict(), "configuration_sha256": self.config_sha,
                "assignment_sha256": self.assignment_sha, "pid": os.getpid(), "boot_id": self.boot_id,
                "boot": current["boot"], "issued_at": int(time.time()), "state_sha256": self.state.digest(current),
                "custody": backend_profile(), "network_id": self.config["network_id"], "device_profile": PROFILE,
                "hardware_attested": False, "cog_execution_authorized": False, "group_certificate": group}
        return {"status": body, "signature": self.signer.sign(STATUS_DOMAIN + canonical(body)).hex()}


def run(config_path):
    from .session import require_scope
    require_scope()
    if os.getuid() == 0:
        raise SynProtocolError("Agent process requires a restricted non-root native identity")
    status = Path("/proc/self/status").read_text()
    if "NoNewPrivs:\t1\n" not in status or "CapEff:\t0000000000000000\n" not in status:
        raise SynProtocolError("Agent process requires no_new_privileges and no effective capabilities")
    server = AgentServer(config_path)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: server.stopped.set())
    server.serve()
