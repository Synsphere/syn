"""Pure host policy and WSL bridge tests; no Windows host or secrets required."""
from __future__ import annotations

import hashlib
import os
import importlib.util
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host-custody"))
from syn_host_custody import core


class FakeVault:
    def __init__(self):
        self.values = {}
        self.secrets = {}
        self.allowed = set()

    def has_identity(self, name):
        return name in self.values

    def create_identity(self, name, entropy, identity):
        if name in self.values:
            raise ValueError("Exists")
        self.values[name] = entropy, identity

    def identity(self, name):
        return self.values[name]

    def get_slot(self, service, slot):
        return self.secrets.get((service, slot))

    def set_slot(self, service, slot, value):
        self.secrets[service, slot] = value

    def is_approved(self, *args):
        return args in self.allowed

    def approve_group(self, *args):
        self.allowed.add(args)


@pytest.fixture
def authority(monkeypatch):
    vault = FakeVault()
    public = {"base_public_key_hash": hashlib.sha256(b"public").hexdigest(),
              "base_public_key": b"public".hex()}
    monkeypatch.setattr(core, "public_identity", lambda entropy: public)
    return core.Authority(vault, approve=lambda msg: True), vault, public


def test_host_creation_is_separate_and_refuses_replacement(authority):
    broker, vault, expected = authority
    created = broker.request({"op": "create", "service": "syn.operator", "account": "primary"})
    assert created == expected
    assert vault.has_identity("syn.operator/primary")
    with pytest.raises(ValueError, match="already exists"):
        broker.request({"op": "create", "service": "syn.operator", "account": "primary"})
    with pytest.raises(ValueError, match="Only Syn Operator"):
        broker.request({"op": "create", "service": "syn.agent", "account": "primary"})


def test_root_slots_never_leak_through_generic_keystore(authority):
    broker, vault, _ = authority
    for protected in core.ROOT_SUFFIXES:
        slot = f"primary.operator.{protected}.v1"
        with pytest.raises(ValueError, match="Root HD material"):
            broker.request({"op": "get", "service": "syn.operator", "account": "slot", "slot": slot})
        with pytest.raises(ValueError, match="protected host custody"):
            broker.request({"op": "set", "service": "syn.operator", "account": "slot", "slot": slot, "value": "secret"})
    assert not vault.secrets


def test_nonroot_control_state_is_readable_after_write(authority):
    broker, vault, _ = authority
    slot = "primary.operator.control-state.v1"
    body = {"service": "syn.operator", "account": "slot", "slot": slot}
    assert broker.request({"op": "get", **body}) is None
    assert broker.request({"op": "set", **body, "value": "test-state"}) is True
    assert broker.request({"op": "get", **body}) == "test-state"
    assert vault.secrets == {("syn.operator", slot): "test-state"}


def test_owner_confirmation_is_required_for_host_root(authority):
    broker, vault, _ = authority
    broker.approve = lambda _: False
    with pytest.raises(ValueError, match="declined"):
        broker.request({"op": "create", "service": "syn.operator", "account": "owner"})
    assert not vault.values


def test_host_rejects_generic_signing_and_bad_pin(authority):
    broker, _, identity = authority
    broker.request({"op": "create", "service": "syn.operator", "account": "owner"})
    req = {"op": "sign", "service": "syn.operator", "account": "owner",
           "pin": identity["base_public_key_hash"]}
    with pytest.raises(ValueError, match="Unknown Syn signature domain"):
        broker.request({**req, "payload": b"unscoped".hex()})
    with pytest.raises(ValueError, match="pinned hash"):
        broker.request({**req, "pin": "0"*64, "payload": b"syn.operator-status/1\0{}".hex()})


def test_group_key_requires_host_approval_and_canonical_uuid(authority, monkeypatch):
    broker, vault, identity = authority
    broker.request({"op": "create", "service": "syn.operator", "account": "owner"})
    monkeypatch.setattr(core, "group_key", lambda *a: b"K"*32)
    group = "6f00d39f-4ec6-4567-8596-c176cdaf318f"
    req = {"op": "group_key", "service": "syn.operator", "account": "owner",
           "pin": identity["base_public_key_hash"], "network_id": "syn",
           "group_id": group, "epoch": 1}
    broker.approve = lambda _: False
    with pytest.raises(ValueError, match="declined"):
        broker.request(req)
    assert not vault.allowed
    broker.approve = lambda _: True
    assert broker.request(req) == (b"K"*32).hex()
    assert len(vault.allowed) == 1
    broker.approve = lambda _: False
    assert broker.request(req) == (b"K"*32).hex()  # Retained epoch approval


def test_group_context_and_domain_reject_bad_inputs():
    assert core.canonical({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    for group in ("not-a-group", "00000000-0000-0000-0000-000000000000"):
        with pytest.raises(ValueError):
            core.group_key(b"k"*32, "0"*64, "syn", group, 1)
    for payload in (b"", b"random", b"syn.fake/1\0{}"):
        assert not any(payload.startswith(x) for x in core.SIGN_DOMAINS)


def load_wsl_bridge(monkeypatch):
    # Import only the standalone bridge module, not the broader Syn package.
    synclient = types.ModuleType("syn_client")
    protocol = types.ModuleType("syn_client.operator_protocol")
    class SynProtocolError(ValueError):
        pass
    protocol.SynProtocolError = SynProtocolError
    monkeypatch.setitem(sys.modules, "syn_client", synclient)
    monkeypatch.setitem(sys.modules, "syn_client.operator_protocol", protocol)
    spec = importlib.util.spec_from_file_location("host_custody_bridge", ROOT / "src/syn/operator/host_custody.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_wsl_bridge_forwards_bounded_json_without_shell(monkeypatch):
    module = load_wsl_bridge(monkeypatch)
    monkeypatch.setenv("SYN_HOST_CUSTODY_PIPE", "SynHostCustody-" + "a"*32)
    monkeypatch.setenv("SYN_HOST_CUSTODY_CLIENT_WIN", "C:\\Syn Root\\pipe-client.ps1")
    monkeypatch.setenv("SYN_HOST_CUSTODY_POWERSHELL", "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    captured = []
    def fake_run(args, **kw):
        captured.append((args, kw))
        return types.SimpleNamespace(returncode=0, stdout='{"ok":true,"result":"value"}')
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.HostKeyringProxy().get_password("syn.operator", "owner.operator.control-state.v1") == "value"
    args, kwargs = captured[0]
    assert args[-3:] == ["C:\\Syn Root\\pipe-client.ps1", "-Pipe", "SynHostCustody-"+"a"*32]
    assert kwargs["input"] == '{"account":"slot","op":"get","service":"syn.operator","slot":"owner.operator.control-state.v1"}\n'
    assert kwargs["capture_output"] and kwargs["timeout"] == 30


def test_bridge_fails_closed_when_windows_broker_unavailable(monkeypatch):
    module = load_wsl_bridge(monkeypatch)
    monkeypatch.setenv("SYN_HOST_CUSTODY_PIPE", "SynHostCustody-"+"a"*32)
    monkeypatch.setenv("SYN_HOST_CUSTODY_CLIENT_WIN", "C:\\client.ps1")
    monkeypatch.setenv("SYN_HOST_CUSTODY_POWERSHELL", "/mnt/c/powershell.exe")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=4, stdout="", stderr="pipe missing"))
    with pytest.raises(ValueError, match="no fallback"):
        module.HostKeyringProxy().get_password("syn.operator", "key")

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell bootstrap simulation")
def test_wsl_bootstrap_uses_host_broker_without_secret_service(tmp_path):
    import shutil
    root = tmp_path / "Syn worktree"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts" / "bootstrap-wsl.sh", scripts / "bootstrap-wsl.sh")
    home = tmp_path / "home"
    binaries = home / ".local" / "bin"
    binaries.mkdir(parents=True)

    def tool(name, body):
        p = binaries / name
        p.write_text("#!/bin/sh\n"+body)
        p.chmod(0o700)
        return p

    tool("id", 'case "$1" in -u) echo 1000 ;; -un) echo syn ;; *) /usr/bin/id "$@" ;; esac\n')
    tool("dpkg-query", "echo 'install ok installed'\n")
    tool("uv", 'if [ "$1" = --version ]; then exit 0; fi\nprintf "%s\\n" "$@" > "$HOME/uv-args"\n')
    bridge = tool("host-powershell", 'echo \'{"ok":true,"result":{"ready":true}}\'\n')
    import subprocess
    env = dict(os.environ, HOME=str(home), SYN_DATABASE_URL="postgresql+psycopg:///syn_syn",
        SYN_BOOTSTRAP_OFFLINE="1", SYN_HOST_CUSTODY_PIPE="SynHostCustody-"+"a"*32,
        SYN_HOST_CUSTODY_POWERSHELL=str(bridge),
        SYN_HOST_CUSTODY_CLIENT_WIN=r"C:\Syn root\host-custody\pipe-client.ps1")
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    env.pop("SYN_BOOTSTRAP_SECRETS_READY", None)
    result = subprocess.run(["sh", str(scripts / "bootstrap-wsl.sh")],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "Using Windows host DPAPI custody" in result.stdout
    assert "Starting a private D-Bus session" not in result.stdout
    assert (home / "uv-args").read_text().splitlines()[-1].endswith("config.toml")
