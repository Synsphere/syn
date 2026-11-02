"""Owner-approved host-bound virtual device profile; no hardware attestation.

The OS-keystore seed is distinct from the temporary signing seed. The raw ID is
only placed inside the authenticated encrypted operator-directed enrollment.
This Linux/WSL profile defines one virtual device per protected Agent account.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import sys

from syn.operator.custody import _agent_key_lock, _native_backend, private_directory, private_read
from syn.operator.state import write_new
from syn_client.operator_protocol import SynProtocolError, canonical, exact, hex_bytes, parse
from .temporary import AgentTemporaryOsSigner, create_agent_temporary_key

PROFILE = "syn.host-bound-virtual-device/1"
DOMAIN = b"syn.virtual-device.host-binding.v1\0"


def host_binding():
    if sys.platform != "linux":
        raise SynProtocolError("Virtual-device profile currently requires Linux or WSL")
    path = Path("/etc/machine-id")
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
            or info.st_size > 128 or info.st_nlink != 1):
        raise SynProtocolError("Native host identity has unsafe ownership or type")
    raw = path.read_bytes().strip()
    if not re.fullmatch(b"[0-9a-f]{32}", raw) or raw == b"0" * 32:
        raise SynProtocolError("Native host machine identity is absent or invalid")
    return hashlib.sha256(DOMAIN + bytes.fromhex(raw.decode("ascii"))).hexdigest()


def initialize(directory, *, service, account, expected_operator, network_id, group_id, group_epoch, approved_profile):
    from syn_client.group import group_id as check_group, group_epoch as check_epoch
    check_group(group_id)
    check_epoch(group_epoch)
    if approved_profile != PROFILE:
        raise SynProtocolError("An explicit owner-approved virtual-device policy is required")
    private_directory(directory)
    hex_bytes(expected_operator, 32, "independent operator pin")
    if not isinstance(network_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", network_id):
        raise SynProtocolError("Invalid Agent network")
    if len(os.fsencode(directory / "agent.sock")) > 103:
        raise SynProtocolError("Agent socket path is too long")
    with _agent_key_lock(directory, service, account + ".provision"):
        if any(directory.iterdir()):
            raise SynProtocolError("Agent directory is not empty; no identity replacement")
        backend = _native_backend()
        slot = account + ".agent.virtual-device.v1"
        if backend.get_password(service, slot) is not None:
            raise SynProtocolError("Virtual device exists or was partially initialized")
        write_new(directory / "initializing.json", canonical({"profile": "syn.agent-initializing/1"}))
        temporary = create_agent_temporary_key(service=service, account=account, lock_dir=directory)
        host = host_binding()
        # Independent OS entropy; never temporary signing-key material.
        device = {"profile": PROFILE, "host_binding": host, "device_secret": os.urandom(32).hex(),
                  "state_dir": str(directory), "owner_uid": os.getuid()}
        encoded = canonical(device).decode("ascii")
        backend.set_password(service, slot, encoded)
        if backend.get_password(service, slot) != encoded:
            raise SynProtocolError("Native virtual-device custody readback failed")
        config = {"profile": "syn.agent-config/1", "state_dir": str(directory),
                  "socket": str(directory / "agent.sock"), "keyring_service": service,
                  "keyring_account": account, "expected_operator": expected_operator,
                  "network_id": network_id, "temporary_public_key_hash": temporary["temporary_public_key_hash"],
                  "device_profile": PROFILE, "host_binding": host, "owner_uid": os.getuid(),
                  "group_id": group_id, "group_epoch": group_epoch}
        write_new(directory / "agent.json", canonical(config))
        return {"profile": "syn.agent-created/1", "temporary_public_key": temporary["temporary_public_key"],
                "configuration_sha256": hashlib.sha256(canonical(config)).hexdigest(),
                "device_profile": PROFILE, "hardware_attested": False, "cog_execution_authorized": False}


def load_config(path):
    raw = private_read(path)
    config = exact(parse(raw), {"profile", "state_dir", "socket", "keyring_service", "keyring_account",
                   "expected_operator", "network_id", "temporary_public_key_hash", "device_profile",
                   "host_binding", "owner_uid", "group_id", "group_epoch"}, "Agent configuration")
    if (config["profile"] != "syn.agent-config/1" or config["state_dir"] != str(path.parent)
            or config["socket"] != str(path.parent / "agent.sock") or config["owner_uid"] != os.getuid()
            or config["device_profile"] != PROFILE or config["host_binding"] != host_binding()):
        raise SynProtocolError("Agent configuration differs from its host, owner or virtual-device policy")
    hex_bytes(config["expected_operator"], 32, "operator pin")
    hex_bytes(config["temporary_public_key_hash"], 32, "temporary identity")
    from syn_client.group import group_id, group_epoch
    group_id(config["group_id"])
    group_epoch(config["group_epoch"])
    return config, hashlib.sha256(raw).hexdigest()


def device_id(config):
    backend = _native_backend()
    value = backend.get_password(config["keyring_service"], config["keyring_account"] + ".agent.virtual-device.v1")
    if value is None:
        raise SynProtocolError("Virtual device is absent; no automatic replacement")
    body = exact(parse(value.encode("ascii")), {"profile", "host_binding", "device_secret", "state_dir", "owner_uid"}, "virtual device")
    if (body["profile"] != PROFILE or body["host_binding"] != config["host_binding"]
            or body["host_binding"] != host_binding() or body["state_dir"] != config["state_dir"]
            or body["owner_uid"] != os.getuid()):
        raise SynProtocolError("Virtual device belongs to another host, account or directory")
    return hashlib.sha256(DOMAIN + bytes.fromhex(body["host_binding"])
                          + hex_bytes(body["device_secret"], 32, "virtual-device seed")).digest()


def temporary_signer(config):
    return AgentTemporaryOsSigner(config["keyring_service"], config["keyring_account"],
                                  bytes.fromhex(config["temporary_public_key_hash"]))
