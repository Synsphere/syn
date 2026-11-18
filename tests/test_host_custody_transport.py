"""Queue/response budgets and fail-closed, non-replaying host transport."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace
import uuid

import pytest

from syn.operator import host_custody as bridge
from syn_client.operator_protocol import SynProtocolError


CLIENT = Path(__file__).resolve().parents[1] / "host-custody/pipe-client.ps1"
POWERSHELL = shutil.which("powershell.exe") if os.name == "nt" else None


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("SYN_HOST_CUSTODY_PIPE", "SynHostCustody-" + "a" * 32)
    monkeypatch.setenv("SYN_HOST_CUSTODY_CLIENT_WIN", r"C:\Syn\host-custody\pipe-client.ps1")
    monkeypatch.setenv("SYN_HOST_CUSTODY_POWERSHELL", str(Path("powershell.exe").resolve()))
    return []


@pytest.mark.parametrize("operation,response_seconds", [
    ("identity", 30), ("get", 30), ("set", 30), ("create", 300),
    ("group_key", 300), ("dag_anchor", 300), ("peer_record", 300), ("sign", 300),
])
def test_runtime_queue_budget_is_separate_from_operation_deadline(configured, monkeypatch, operation, response_seconds):
    def run(arguments, **options):
        configured.append((arguments, options))
        return SimpleNamespace(returncode=0, stdout='{"ok":true,"result":true}', stderr="")
    monkeypatch.setattr(bridge.subprocess, "run", run)
    assert bridge.request({"op": operation}) is True
    assert len(configured) == 1
    arguments, options = configured[0]
    assert arguments[arguments.index("-ConnectTimeoutMilliseconds") + 1] == "300000"
    assert arguments[arguments.index("-ResponseTimeoutMilliseconds") + 1] == str(response_seconds * 1000)
    assert options["timeout"] == 300 + response_seconds + 10
    assert json.loads(options["input"]) == {"op": operation}
    assert not options.get("shell")


@pytest.mark.parametrize("marker,expected", [
    ("connect-timeout", "busy or unavailable before submission"),
    ("connect-denied", "denied access"),
    ("connect-failed", "could not be opened"),
    ("write-timeout", "outcome is unconfirmed"),
    ("write-failed", "outcome is unconfirmed"),
    ("read-timeout", "response timed out"),
    ("read-failed", "could not be read"),
    ("response-invalid", "empty or oversized"),
    ("unknown-private-material", "no trusted transport diagnostic"),
])
def test_fixed_transport_diagnostics_never_echo_stderr_or_replay_mutations(configured, monkeypatch, marker, expected):
    def run(*args, **kwargs):
        configured.append((args, kwargs))
        return SimpleNamespace(returncode=4, stdout="", stderr=(
            "secret-token\nSYN_HOST_CUSTODY_FAILURE:" + marker + "\nraw request=secret-token"))
    monkeypatch.setattr(bridge.subprocess, "run", run)
    with pytest.raises(SynProtocolError, match=expected) as error:
        bridge.request({"op": "set", "value": "secret-token"})
    assert "secret-token" not in str(error.value)
    assert "no fallback to WSL secrets" in str(error.value)
    assert len(configured) == 1


@pytest.mark.parametrize("failure", ["timeout", "start", "malformed", "rejected", "oversized"])
def test_ambiguous_or_rejected_operations_are_not_resubmitted(configured, monkeypatch, failure):
    def run(*args, **kwargs):
        configured.append((args, kwargs))
        if failure == "timeout":
            raise subprocess.TimeoutExpired("bridge", 1)
        if failure == "start":
            raise OSError("fixture interop unavailable")
        stdout = {"malformed": "{", "rejected": '{"ok":false,"error":"declined"}',
                  "oversized": "x" * 140001}[failure]
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    monkeypatch.setattr(bridge.subprocess, "run", run)
    with pytest.raises(SynProtocolError):
        bridge.request({"op": "create"})
    assert len(configured) == 1


@pytest.mark.skipif(POWERSHELL is None, reason="Native Windows PowerShell required")
def test_native_pipe_client_waits_behind_a_request_longer_than_old_750ms(tmp_path, monkeypatch):
    """Two real clients, one serial pipe; no real broker/vault or system changes."""
    pipe = "SynHostCustody-" + uuid.uuid4().hex
    ready, entered = tmp_path / "ready", tmp_path / "entered"
    server = tmp_path / "server.ps1"
    server.write_text(r'''
param($Pipe, $Ready, $Entered)
$ErrorActionPreference = 'Stop'
for ($i = 0; $i -lt 2; $i++) {
    $server = [IO.Pipes.NamedPipeServerStream]::new($Pipe, [IO.Pipes.PipeDirection]::InOut, 1)
    try {
        if ($i -eq 0) { [IO.File]::WriteAllText($Ready, 'ready') }
        $server.WaitForConnection()
        $reader = [IO.StreamReader]::new($server, [Text.Encoding]::UTF8)
        $null = $reader.ReadLine()
        if ($i -eq 0) {
            [IO.File]::WriteAllText($Entered, 'entered')
            Start-Sleep -Milliseconds 1500
        }
        $writer = [IO.StreamWriter]::new($server, [Text.UTF8Encoding]::new($false), 4096, $true)
        $writer.WriteLine('{"ok":true,"result":{"ready":true}}')
        $writer.Flush()
        $server.WaitForPipeDrain()
        $server.Disconnect()
    } finally { $server.Dispose() }
}
''', encoding="utf-8")
    monkeypatch.setenv("SYN_HOST_CUSTODY_PIPE", pipe)
    monkeypatch.setenv("SYN_HOST_CUSTODY_CLIENT_WIN", str(CLIENT))
    monkeypatch.setenv("SYN_HOST_CUSTODY_POWERSHELL", POWERSHELL)
    monkeypatch.setattr(bridge, "CONNECT_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(bridge, "RESPONSE_TIMEOUT_SECONDS", 10)
    process = subprocess.Popen([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(server), "-Pipe", pipe, "-Ready", str(ready), "-Entered", str(entered)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wait_for(path):
        deadline = time.monotonic() + 10
        while not path.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise AssertionError("Native pipe fixture did not become ready")
            time.sleep(.01)

    try:
        wait_for(ready)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(bridge.request, {"op": "health"})
            wait_for(entered)
            second = pool.submit(bridge.request, {"op": "health"})
            assert first.result(timeout=20) == {"ready": True}
            assert second.result(timeout=20) == {"ready": True}
        _, errors = process.communicate(timeout=10)
        assert process.returncode == 0, errors
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
