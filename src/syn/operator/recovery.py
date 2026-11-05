"""Explicit native operator recovery from a pinned, encrypted offline checkpoint."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from mnemonic import Mnemonic

from syn_client.consensus import load_document
from syn_client.operator_protocol import SynProtocolError, canonical, exact, hex_bytes

from .adoption_state import EnrollmentJournal
from .custody import (
    _agent_key_lock,
    _entry,
    _native_backend,
    backend_profile,
    private_directory,
    private_read,
)
from .hd_keys import SLOTS, _derive, _load_operator, _public, _read, _write_identity
from .lan_inbox import LanInbox
from .service import CONFIG_PROFILE, OperatorServer, load_config
from .state import OperatorState, write_new
from .unlock import _password, _private_terminal

PROFILE = "syn.operator-recovery/1"
MAGIC = b"syn.operator-recovery/1\0"
HEADER_BYTES = len(MAGIC) + 32 + 12
MAX_BACKUP_BYTES = 150_000
MAX_STATE_BYTES = 48_000
RECOVERY_SERVICE = "syn.operator.recovery"
RECEIVER_SUFFIX = ".operator.enrollment-receiver.v1"


def _states(directory, service, account, base_hash):
    identity = directory, service, account, base_hash
    return {
        "control": OperatorState(*identity),
        "adoption": EnrollmentJournal(*identity),
        "lan": LanInbox(*identity),
    }


def _existing_state(state, backend):
    anchor_present = backend.get_password(state.service, state.slot) is not None
    if anchor_present or state.path.exists() or state.path.is_symlink():
        # An unfinished transition or a path without its anchor cannot be exported.
        return state.read()
    return None


def _validate_states(states, bodies, receiver):
    exact(bodies, set(states), "recovery state set")
    for name, state in states.items():
        body = bodies[name]
        if name == "control" or body is not None:
            state._validate(body)
            if len(canonical(body)) > MAX_STATE_BYTES:
                raise SynProtocolError("Recovery journal exceeds its native limit")
    if bodies["control"]["socket"] is not None:
        raise SynProtocolError("Recovery requires a cleanly stopped operator")
    if (receiver is None) != (bodies["adoption"] is None):
        raise SynProtocolError("Recovery enrollment receiver and journal disagree")
    if receiver is not None:
        hex_bytes(receiver, 32, "enrollment receiver")
    if bodies["lan"] is not None and bodies["adoption"] is None:
        raise SynProtocolError("Recovery LAN inbox has no adoption journal")


def transition_preflight(config_path: Path) -> dict[str, object]:
    """Read the stopped operator's anchored state without exporting custody material."""
    config, _ = load_config(config_path)
    directory = config_path.parent
    service, account = config["keyring_service"], config["keyring_account"]
    with _agent_key_lock(directory, service, account + ".operator"):
        server = OperatorServer(config_path)
        if server.config != config:
            raise SynProtocolError("Operator configuration changed before transition preflight")
        if server.path.exists() or server.path.is_symlink():
            raise SynProtocolError("Operator socket still exists; stop it cleanly before preflight")
        backend = _native_backend()
        states = _states(directory, service, account, server.expected_hash)
        bodies = {name: _existing_state(state, backend) for name, state in states.items()}
        receiver = backend.get_password(service, account + RECEIVER_SUFFIX)
        _validate_states(states, bodies, receiver)
        return {
            "profile": "syn.operator-transition-preflight/1",
            "base_public_key_hash": server.expected_hash,
            "configuration_sha256": server.config_sha,
            "state_sha256": {
                name: states[name].digest(body) for name, body in bodies.items() if body is not None
            },
            "agent_activation_authorized": False,
        }


def _cipher(entropy, header):
    salt = header[len(MAGIC) : len(MAGIC) + 32]
    return AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=MAGIC).derive(entropy))


def _backup(config_path, output, terminal):
    config, _ = load_config(config_path)
    directory = config_path.parent
    service, account = config["keyring_service"], config["keyring_account"]
    pin = config["expected_base_public_key_hash"]
    private_directory(output.parent)
    with _agent_key_lock(directory, service, account + ".operator"):
        phrase = f"BACKUP {pin[:12]}"
        terminal.write(
            f"Type {phrase} to create an encrypted checkpoint and show its private phrase: "
        )
        terminal.flush()
        if terminal.readline(128).strip() != phrase:
            raise SynProtocolError("Operator recovery backup permission refused")
        server = OperatorServer(config_path)
        if server.config != config:
            raise SynProtocolError("Operator configuration changed before backup")
        if server.path.exists() or server.path.is_symlink():
            raise SynProtocolError("Operator socket still exists; stop it cleanly before backup")
        backend = _native_backend()
        values, _ = _load_operator(service, account)
        states = _states(directory, service, account, pin)
        bodies = {name: _existing_state(state, backend) for name, state in states.items()}
        receiver = backend.get_password(service, account + RECEIVER_SUFFIX)
        _validate_states(states, bodies, receiver)
        snapshot = {
            "profile": PROFILE,
            "base_hash": pin,
            "network_id": config["network_id"],
            "source_service": service,
            "source_account": account,
            "states": bodies,
            "enrollment_receiver": receiver,
        }
        entropy = values["entropy"]
        header = MAGIC + os.urandom(32) + os.urandom(12)
        raw = header + _cipher(entropy, header).encrypt(header[-12:], canonical(snapshot), header)
        if len(raw) > MAX_BACKUP_BYTES:
            raise SynProtocolError("Recovery backup exceeds its file limit")
        write_new(output, raw)
        terminal.write(
            "Private recovery phrase:\n" + Mnemonic("english").to_mnemonic(entropy) + "\n"
        )
        terminal.flush()
    return {
        "profile": PROFILE,
        "base_public_key_hash": pin,
        "backup_sha256": hashlib.sha256(raw).hexdigest(),
        "state_sha256": {
            name: states[name].digest(body) for name, body in bodies.items() if body is not None
        },
    }


def backup_operator(config_path: Path, output: Path):
    with _private_terminal() as terminal:
        return _backup(config_path, output, terminal)


def _read_snapshot(raw, entropy, pin):
    header = raw[:HEADER_BYTES]
    try:
        plaintext = _cipher(entropy, header).decrypt(header[-12:], raw[HEADER_BYTES:], header)
    except InvalidTag:
        raise SynProtocolError("Recovery checkpoint authentication failed") from None
    snapshot = exact(
        load_document(plaintext.decode("ascii")),
        {
            "profile",
            "base_hash",
            "network_id",
            "source_service",
            "source_account",
            "states",
            "enrollment_receiver",
        },
        "operator recovery checkpoint",
    )
    if (
        canonical(snapshot) != plaintext
        or snapshot["profile"] != PROFILE
        or snapshot["base_hash"] != pin
        or not isinstance(snapshot["network_id"], str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", snapshot["network_id"])
    ):
        raise SynProtocolError("Recovery checkpoint differs from the selected operator")
    _entry(snapshot["source_service"], snapshot["source_account"])
    return snapshot


def _restore(directory, service, account, backup, pin, digest, terminal):
    _entry(service, account)
    hex_bytes(pin, 32, "operator base key pin")
    hex_bytes(digest, 32, "independent recovery checkpoint pin")
    private_directory(directory)
    if len(os.fsencode(directory / "operator.sock")) > 103:
        raise SynProtocolError("Operator state path exceeds native socket limits")
    raw = private_read(backup, maximum=MAX_BACKUP_BYTES)
    if (
        len(raw) <= HEADER_BYTES + 16
        or not raw.startswith(MAGIC)
        or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), digest)
    ):
        raise SynProtocolError("Recovery file differs from the independently selected checkpoint")

    confirmation = f"RESTORE {pin[:12]} {digest[:12]}"
    terminal.write("Fence the original operator and confirm the latest trusted checkpoint.\n")
    terminal.write(f"Type {confirmation} to restore: ")
    terminal.flush()
    if terminal.readline(128).strip() != confirmation:
        raise SynProtocolError("Operator recovery permission refused")
    phrase = _password("Operator recovery phrase (private, no echo): ", terminal)
    codec = Mnemonic("english")
    if len(phrase.split()) != 24 or not codec.check(phrase):
        raise SynProtocolError("Invalid 24-word operator recovery phrase")
    entropy = bytes(codec.to_entropy(phrase))
    values = _derive(entropy)
    public = _public(values)
    if not hmac.compare_digest(public["base_public_key_hash"], pin):
        raise SynProtocolError("Recovery phrase does not match the independently pinned operator")

    snapshot = _read_snapshot(raw, entropy, pin)
    states = _states(directory, service, account, pin)
    bodies, receiver = snapshot["states"], snapshot["enrollment_receiver"]
    _validate_states(states, bodies, receiver)
    config = {
        "profile": CONFIG_PROFILE,
        "state_dir": str(directory),
        "socket": str(directory / "operator.sock"),
        "keyring_service": service,
        "keyring_account": account,
        "expected_base_public_key_hash": pin,
        "network_id": snapshot["network_id"],
    }
    with (
        _agent_key_lock(directory, RECOVERY_SERVICE, pin),
        _agent_key_lock(directory, service, account + ".provision"),
        _agent_key_lock(directory, service, account + ".operator"),
    ):
        backend = _native_backend()
        if any(directory.iterdir()) or any(
            _read(backend, service, account, name) is not None for name in SLOTS
        ):
            raise SynProtocolError("Recovery destination already exists or is incomplete")
        slots = [state.slot for state in states.values()] + [account + RECEIVER_SUFFIX]
        if any(backend.get_password(service, slot) is not None for slot in slots):
            raise SynProtocolError("Recovery destination retains native enrollment state")
        if (
            backend.get_password(RECOVERY_SERVICE, pin) is not None
            or _read(
                backend,
                snapshot["source_service"],
                snapshot["source_account"],
                "entropy",
            )
            is not None
        ):
            raise SynProtocolError(
                "This native store already holds the recovered or original operator"
            )
        # A failed restore retains its local claim and partial destination. It cannot be
        # retried over ambiguous native state. The operator config is published last.
        write_new(
            directory / "initializing.json",
            canonical({"profile": PROFILE, "backup_sha256": digest}),
        )
        backend.set_password(RECOVERY_SERVICE, pin, digest)
        if backend.get_password(RECOVERY_SERVICE, pin) != digest:
            raise SynProtocolError("Native recovery claim readback failed")
        _write_identity(backend, service, account, values, public)
        if receiver is not None:
            backend.set_password(service, account + RECEIVER_SUFFIX, receiver)
            if backend.get_password(service, account + RECEIVER_SUFFIX) != receiver:
                raise SynProtocolError("Native enrollment receiver readback failed")
        for name, state in states.items():
            body = bodies[name]
            if body is not None:
                state._create(body)
                if state.read() != body:
                    raise SynProtocolError("Recovered state differs from the selected checkpoint")
        write_new(directory / "operator.json", canonical(config))
    return {
        "profile": PROFILE,
        "identity": public,
        "backup_sha256": digest,
        "configuration_sha256": hashlib.sha256(canonical(config)).hexdigest(),
        "state_sha256": {
            name: states[name].digest(body) for name, body in bodies.items() if body is not None
        },
        "custody": backend_profile(),
        "agent_activation_authorized": False,
    }


def recover_operator(
    directory, *, service, account, backup, expected_base_hash, expected_backup_sha256
):
    with _private_terminal() as terminal:
        return _restore(
            directory,
            service,
            account,
            backup,
            expected_base_hash,
            expected_backup_sha256,
            terminal,
        )
