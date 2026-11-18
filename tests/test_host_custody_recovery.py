"""Recovery selection guards plus opt-in native Windows process tests.

Linux tests inspect safety-critical boundaries only; native behavior is qualified
by the Windows tests, which start disposable sleeping module fixtures, not a
real vault or broker. No user identity or existing broker is stopped.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "Stop-SynHostCustody.ps1"
PIPE = "SynHostCustody-" + "f" * 32


def test_recovery_requires_explicit_pid_and_has_no_file_or_blanket_process_mutation():
    source = SCRIPT.read_text()
    assert "ContainsKey('ProcessId')" in source
    listing = source.split("if (-not $PSBoundParameters.ContainsKey('ProcessId'))", 1)[1].split("$ownedProcess = $null", 1)[0]
    assert "return" in listing and ".Kill()" not in listing
    for forbidden in ("Stop-Process", "taskkill", "Remove-Item", "DeleteFile", "vault-v1.dpapi"):
        assert forbidden not in source
    assert "SupportsShouldProcess = $true" in source
    assert "ConfirmImpact = 'Medium'" in source
    assert "Read-Host" not in source


def test_recovery_parses_real_windows_argv_and_checks_current_user():
    source = SCRIPT.read_text()
    assert "CommandLineToArgvW" in source and "LocalFree(memory)" in source
    assert "$arguments[1] -cne '-m'" in source
    assert "$arguments[2] -cne 'syn_host_custody.broker'" in source
    assert "\\ASynHostCustody-[a-f0-9]{32}\\z" in source
    assert "-MethodName GetOwnerSid" in source
    assert "$owner.ReturnValue -ne 0 -or $owner.Sid -cne $currentSid" in source
    assert "WindowsIdentity]::GetCurrent().User.Value" in source


def test_recovery_pins_process_before_revalidation_and_kills_only_that_object():
    source = SCRIPT.read_text().split("$ownedProcess = $null", 1)[1]
    pin = source.index("$null = $ownedProcess.Handle")
    query = source.index('Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId"')
    owner = source.index("$record = Get-SynBrokerRecord $native")
    creation = source.index("if ($nativeTicks -ne $cimTicks)")
    decision = source.index("$PSCmdlet.ShouldProcess")
    kill = source.index("$ownedProcess.Kill()")
    assert pin < query < owner < creation < decision < kill
    assert "$ownedProcess.WaitForExit(5000)" in source
    assert "$ownedProcess.Dispose()" in source


POWERSHELL = shutil.which("powershell.exe") if sys.platform == "win32" else None
native = pytest.mark.skipif(POWERSHELL is None, reason="Native Windows PowerShell and CIM are required")


def powershell(*arguments):
    return subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", *arguments], capture_output=True, text=True, timeout=20)


@contextmanager
def module_process(tmp_path, *, arguments=None, code=None):
    package = tmp_path / "syn_host_custody"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "broker.py").write_text("import time\ntime.sleep(60)\n")
    command = [sys.executable]
    command.extend(["-c", code] if code is not None else
                   ["-m", "syn_host_custody.broker", *(arguments or ["--pipe", PIPE])])
    child = subprocess.Popen(command, cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        time.sleep(.1)
        assert child.poll() is None, child.stderr.read().decode()
        yield child
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
        child.stderr.close()


@native
def test_native_listing_and_whatif_preserve_disposable_broker(tmp_path):
    with module_process(tmp_path) as child:
        quoted = str(SCRIPT).replace("'", "''")
        result = powershell("-Command", f"ConvertTo-Json -Compress -InputObject @(& '{quoted}')")
        assert result.returncode == 0, result.stderr
        records = json.loads(result.stdout)
        assert any(row["ProcessId"] == child.pid and row["Pipe"] == PIPE for row in records)
        result = powershell("-File", str(SCRIPT), "-ProcessId", str(child.pid), "-WhatIf")
        assert result.returncode == 0, result.stderr
        assert child.poll() is None


@native
def test_native_explicit_pid_stops_only_selected_disposable_broker(tmp_path):
    with module_process(tmp_path) as child:
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            result = powershell("-File", str(SCRIPT), "-ProcessId", str(child.pid))
            assert result.returncode == 0, result.stderr
            child.wait(timeout=5)
            assert unrelated.poll() is None
        finally:
            if unrelated.poll() is None:
                unrelated.terminate()
            unrelated.wait(timeout=5)


@native
def test_native_lookalike_command_text_is_not_a_broker(tmp_path):
    code = "import time; time.sleep(60) # -m syn_host_custody.broker --pipe " + PIPE
    with module_process(tmp_path, code=code) as child:
        result = powershell("-File", str(SCRIPT), "-ProcessId", str(child.pid))
        assert result.returncode != 0
        assert "not a verified current-user Syn host-custody broker" in result.stderr
        assert child.poll() is None


@native
def test_native_ambiguous_pipe_arguments_are_rejected(tmp_path):
    with module_process(tmp_path, arguments=["--pipe", PIPE, "--pipe", PIPE]) as child:
        result = powershell("-File", str(SCRIPT), "-ProcessId", str(child.pid))
        assert result.returncode != 0
        assert child.poll() is None
