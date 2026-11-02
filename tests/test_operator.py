"""Synthetic fault tests; actual native custody/provider qualification is separate."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from syn.operator import custody, hd_keys, state as state_module
from syn.operator.service import OperatorServer, initialize, load_config
from syn.operator.state import OperatorState
from syn_client.operator import operator_status, verify_status
from syn_client.operator_protocol import SynProtocolError, canonical


class MemoryBackend:
    """Explicit fake: never supplies evidence of OS credential-store protection."""
    def __init__(self):
        self.values = {}
        self.fail = False

    def get_password(self, service, account):
        if self.fail:
            raise OSError("simulated locked backend")
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        if self.fail:
            raise OSError("simulated locked backend")
        self.values[service, account] = value


class FixtureWallet:
    """Ordinary synthetic Ed25519 API shape; this is NOT an HD implementation."""
    def __init__(self, entropy, path="m"):
        self.entropy = entropy
        seed = hashlib.sha256(entropy + path.encode()).digest()
        self.xprivate_key = seed + seed
        self.public_key = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.chain_code = hashlib.sha256(path.encode()).digest()

    @classmethod
    def from_entropy(cls, entropy, passphrase=""):
        assert passphrase == ""
        return cls(bytes.fromhex(entropy))

    def derive_from_path(self, path):
        return FixtureWallet(self.entropy, path)


class FixtureExtendedKey:
    def __init__(self, raw):
        self.private = Ed25519PrivateKey.from_private_bytes(raw[:32])

    def sign(self, raw):
        return self.private.sign(raw)


@pytest.fixture
def installed(monkeypatch):
    if os.name != "posix":
        pytest.skip("Native directory/lock/socket cases run on Linux/macOS")
    with tempfile.TemporaryDirectory(prefix="syn-op-", dir=Path("/tmp").resolve()) as name:
        root = Path(name).resolve()
        root.chmod(0o700)
        home = root / "home"
        home.mkdir(mode=0o700)
        node = root / "node"
        node.mkdir(mode=0o700)
        backend = MemoryBackend()
        monkeypatch.setattr(custody, "native_home", lambda: home)
        for module in (hd_keys, state_module):
            monkeypatch.setattr(module, "_native_backend", lambda: backend)
        import syn.operator.service as service_module
        monkeypatch.setattr(service_module, "_native_backend", lambda: backend)
        monkeypatch.setattr(hd_keys, "_pycardano", lambda: (FixtureWallet, FixtureExtendedKey))
        created = initialize(node, service="syn.test", account="fixture", network_id="test-net")
        yield node, backend, created


def test_first_creation_returns_public_pin_and_never_recreates_existing_identity(installed):
    node, backend, created = installed
    assert created["identity"]["base_path"] == "m/6969'/420'/0'/0/0"
    assert created["identity"]["root_path"] == "m/6969'/420'"
    assert created["identity"]["base_public_key_hash"] != created["identity"]["root_public_key_hash"]
    before = dict(backend.values)
    with pytest.raises(SynProtocolError, match="not empty"):
        initialize(node, service="syn.test", account="fixture", network_id="test-net")
    assert backend.values == before
    assert set(created) == {"profile", "identity", "configuration_sha256", "custody", "agent_activation_authorized"}
    assert set(created["identity"]) == {"schema_version", "type_ref", "profile", "root_path", "base_path",
                                      "root_public_key", "base_public_key", "root_public_key_hash", "base_public_key_hash"}


def test_anchored_state_refuses_file_rollback_missing_anchor_and_path_copy(installed):
    node, backend, created = installed
    store = OperatorState(node, "syn.test", "fixture", created["identity"]["base_public_key_hash"])
    old = store.path.read_bytes()
    store.commit({**store.read(), "boot": 1})
    store.path.write_bytes(old)
    with pytest.raises(SynProtocolError, match="rolled back"):
        store.read()
    store.path.unlink()
    with pytest.raises(SynProtocolError):
        store.read(recover=True)
    backend.values.pop((store.service, store.slot))
    with pytest.raises(SynProtocolError, match="absent"):
        store.read()


@pytest.mark.parametrize("after_replace", [False, True])
def test_interrupted_sealed_transition_recovers_exact_state(installed, monkeypatch, after_replace):
    node, _, created = installed
    store = OperatorState(node, "syn.test", "fixture", created["identity"]["base_public_key_hash"])
    original = store._replace
    target = {**store.read(), "boot": 1}

    def fail(body):
        if after_replace:
            original(body)
        raise OSError("simulated process death")

    monkeypatch.setattr(store, "_replace", fail)
    with pytest.raises(OSError):
        store.commit(target)
    monkeypatch.setattr(store, "_replace", original)
    with pytest.raises(SynProtocolError, match="unfinished"):
        store.read()
    assert store.read(recover=True) == target
    assert store.read() == target


def test_real_socket_challenge_and_restart_preserve_synthetic_fixture_identity(installed):
    node, _, created = installed
    from syn.release import runtimeContract
    base = created["identity"]["base_public_key_hash"]
    config_sha = load_config(node / "operator.json")[1]
    previous_boot = 0
    for _ in range(2):
        server = OperatorServer(node / "operator.json")
        failures = []
        def run():
            try:
                server.serve()
            except Exception as exc:
                failures.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while True:
                if failures:
                    raise AssertionError("Synthetic fixture server failed")
                try:
                    value = operator_status(socket_path=node / "operator.sock", expected_hash=base,
                                            expected_version=runtimeContract().runtime_version,
                                            configuration_sha256=config_sha, owner_uid=os.getuid())
                    break
                except (OSError, SynProtocolError):
                    if time.monotonic() > deadline:
                        raise AssertionError("Synthetic fixture failed authenticated readiness") from None
                    time.sleep(0.01)
            assert value["status"]["boot"] > previous_boot
            previous_boot = value["status"]["boot"]
            assert value["status"]["agent_activation_authorized"] is False
            with pytest.raises(SynProtocolError):
                verify_status(value, challenge="ff" * 32, expected_hash=base,
                              expected_version=runtimeContract().runtime_version,
                              configuration_sha256=config_sha)
        finally:
            server.stopped.set()
            thread.join(5)
        assert not thread.is_alive() and not failures
        assert not (node / "operator.sock").exists()


def test_changed_config_and_locked_backend_fence_readiness(installed):
    node, backend, _ = installed
    server = OperatorServer(node / "operator.json")
    backend.fail = True
    with pytest.raises(SynProtocolError):
        server.state.read()
    backend.fail = False
    value = json.loads((node / "operator.json").read_text())
    value["network_id"] = "other"
    (node / "operator.json").write_bytes(canonical(value))
    with pytest.raises(SynProtocolError, match="configuration changed"):
        server.status({"profile": "syn.operator-challenge/1", "challenge": "ab" * 32,
                       "expected_base_public_key_hash": server.expected_hash,
                       "configuration_sha256": server.config_sha})


def test_foreign_socket_is_never_removed(installed):
    import socket
    node, _, _ = installed
    server = OperatorServer(node / "operator.json")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as foreign:
        foreign.bind(str(node / "operator.sock"))
        with pytest.raises(SynProtocolError, match="not the retained"):
            server._clear_stale_socket(server.state.read())
        assert (node / "operator.sock").exists()


def test_missing_native_collection_is_a_read_only_refusal(monkeypatch):
    import sys
    calls = []
    def absent(connection):
        calls.append("read")
        raise OSError("no native collection")
    def forbidden(*args):
        raise AssertionError("Implicit collection creation or prompt")
    monkeypatch.setitem(sys.modules, "secretstorage", SimpleNamespace(
        dbus_init=lambda: object(), Collection=absent, get_default_collection=forbidden))
    with pytest.raises(OSError, match="no native collection"):
        custody._NoninteractiveSecretService().get_password("test", "absent")
    assert calls == ["read"]


def pledge_fixture(signed_pulse, hardware=b"fixture-device-id"):
    from syn.operator.enrollment import OFFER_DOMAIN
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    body = {"schema_version": 2, "type_ref": "synny://type/syn-agent-pledge-offer-v2",
            "temporary_public_key_hash": hashlib.sha256(public).hexdigest(), "hardware_unique_id": hardware.hex(),
            "timestamp_unix_seconds": int(time.time()), "target_master_public_key_hash": signed_pulse["pulse"]["master_public_key_hash"],
            "pulse_challenge": signed_pulse["pulse"]["challenge"]}
    return {"offer": body, "temporary_public_key": public.hex(), "signature": key.sign(OFFER_DOMAIN + canonical(body)).hex()}


def test_enrollment_requires_permission_and_durably_refuses_replay(installed):
    from syn.operator.enrollment import CANDIDATE_DOMAIN, pulse, reserve
    from syn_client.custody import verify_ed25519
    node, _, created = installed
    config = node / "operator.json"
    signed_pulse = pulse(config)
    offer = pledge_fixture(signed_pulse)
    store = OperatorState(node, "syn.test", "fixture", created["identity"]["base_public_key_hash"])
    with pytest.raises(SynProtocolError, match="permission"):
        reserve(config, signed_pulse, offer, 23, approve=lambda _: False)
    assert store.read()["reservations"] == []
    value = reserve(config, signed_pulse, offer, 23, approve=lambda _: True)
    verify_ed25519(bytes.fromhex(value["operator_public_key"]), bytes.fromhex(value["signature"]),
                   CANDIDATE_DOMAIN + canonical(value["candidates"]))
    expected = int.from_bytes(hashlib.sha256(b"fixture-device-id" + bytes([23])).digest()[:3], "big")
    assert value["candidates"]["inheritance"] == expected
    assert len(value["candidates"]["short_public_key_hashes"]) == 8
    committed = store.read()
    assert len(committed["reservations"]) == 1
    assert b"fixture-device-id".hex() not in store.path.read_text()
    with pytest.raises(SynProtocolError, match="already reserved"):
        reserve(config, signed_pulse, offer, 24, approve=lambda _: True)
    with pytest.raises(SynProtocolError, match="append-only"):
        store.commit({**committed, "reservations": []})
    assert store.read() == committed


def test_invalid_pledge_and_expired_pulse_cannot_allocate(installed):
    from syn.operator.enrollment import pulse, reserve
    node, _, _ = installed
    config = node / "operator.json"
    signed_pulse = pulse(config)
    offer = pledge_fixture(signed_pulse)
    offer["offer"]["hardware_unique_id"] = b"tampered-device-id".hex()
    with pytest.raises(Exception):
        reserve(config, signed_pulse, offer, 23, approve=lambda _: pytest.fail("Invalid signature reached permission"))
    signed_pulse["pulse"]["expires_at"] = 1
    with pytest.raises(SynProtocolError, match="stale"):
        reserve(config, signed_pulse, offer, 23, approve=lambda _: pytest.fail("Expired pulse reached permission"))
