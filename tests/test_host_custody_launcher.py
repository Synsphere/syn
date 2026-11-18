"""Broker entry-point lifetime and expected Windows contention diagnostics."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host-custody"))
from syn_host_custody import broker, owner


PIPE = "SynHostCustody-" + "a" * 32


def test_launcher_owner_is_verified_before_vault_and_released_after_server(monkeypatch):
    calls = []

    class Owner:
        def __init__(self, pid, created):
            assert (pid, created) == (123, 456)
        def __enter__(self):
            calls.append("owner-start")
        def __exit__(self, *_):
            calls.append("owner-close")

    monkeypatch.setattr(owner, "OwnerProcess", Owner)
    monkeypatch.setattr(broker, "serve", lambda pipe: calls.append("serve:" + pipe))
    assert broker.main(["--pipe", PIPE, "--owner-pid", "123", "--owner-created", "456"]) == 0
    assert calls == ["owner-start", "serve:" + PIPE, "owner-close"]


def test_owner_failure_never_opens_vault(monkeypatch, capsys):
    class GoneOwner:
        def __init__(self, *_):
            pass
        def __enter__(self):
            raise owner.OwnerProcessError("Syn launcher has already exited")
        def __exit__(self, *_):
            raise AssertionError("A failed context entry is not exited")

    monkeypatch.setattr(owner, "OwnerProcess", GoneOwner)
    monkeypatch.setattr(broker, "serve", lambda _: pytest.fail("Vault must not open without its owner"))
    assert broker.main(["--pipe", PIPE, "--owner-pid", "123", "--owner-created", "456"]) == 4
    assert "already exited" in capsys.readouterr().err


def test_contended_vault_reports_actionable_error_without_traceback(monkeypatch, capsys):
    def busy(_):
        raise broker.VaultInUseError("authority.lock")
    monkeypatch.setattr(broker, "serve", busy)
    assert broker.main(["--pipe", PIPE]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "already using this Windows vault" in captured.err
    assert "Stop-SynHostCustody.ps1" in captured.err
    assert "Do not delete authority.lock or vault-v1.dpapi" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("arguments", [
    ["--pipe", PIPE, "--owner-pid", "12"],
    ["--pipe", PIPE, "--owner-created", "45"],
    ["--pipe", PIPE, "--owner-pid", "0", "--owner-created", "1"],
    ["--pipe", PIPE, "--owner-pid", "12", "--owner-created", "-1"],
    ["--pipe", PIPE, "--owner-pid", str(2**32), "--owner-created", "1"],
    ["--pipe", PIPE, "--owner-pid", "12", "--owner-created", str(2**64)],
    ["--export-recovery", "--owner-pid", "12", "--owner-created", "45"],
])
def test_partial_or_invalid_owner_pin_is_rejected_before_serving(arguments, monkeypatch):
    monkeypatch.setattr(broker, "serve", lambda _: pytest.fail("Invalid owner was accepted"))
    with pytest.raises(SystemExit) as result:
        broker.main(arguments)
    assert result.value.code == 2


def test_server_lifetime_closes_vault_after_pipe_failure(monkeypatch):
    calls = []

    class Vault:
        def __init__(self, directory):
            assert directory == "fixture"
        def __enter__(self):
            calls.append("vault-open")
            return self
        def __exit__(self, *_):
            calls.append("vault-close")

    def fail_pipe(pipe, authority):
        assert pipe == PIPE
        calls.append("pipe")
        raise OSError("Pipe creation failed")

    monkeypatch.setattr(broker, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(broker, "WindowsVault", Vault)
    monkeypatch.setattr(broker, "_serve_pipe", fail_pipe)
    with pytest.raises(OSError, match="Pipe creation"):
        broker.serve(PIPE, "fixture")
    assert calls == ["vault-open", "pipe", "vault-close"]


def test_launcher_tracks_python_and_pins_owner_before_start():
    # Static wiring guard only. Native PowerShell/Windows execution is qualified
    # separately; this assertion does not claim to execute the launcher.
    script = (ROOT / "Start-Syn.ps1").read_text()
    assert "print(sys.executable)" in script
    assert "$psi.FileName = $brokerPython" in script
    assert "--owner-pid " in script and "--owner-created " in script
    assert "ToFileTimeUtc()" in script
    assert "$hostBroker.StandardError.ReadToEndAsync()" in script
    assert "$hostBrokerStarted = $true" in script
    assert "$hostBroker.Kill()" in script and "$hostBroker.WaitForExit(5000)" in script
    assert "taskkill.exe" not in script
    assert "SetEnvironmentVariable($name, $custodyEnvironment[$name], 'Process')" in script
