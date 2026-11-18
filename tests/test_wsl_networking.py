"""Mirrored-mode helper checks; native tests only edit disposable .wslconfig files."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/Set-SynWslNetworking.ps1"
POWERSHELL = shutil.which("powershell.exe") if os.name == "nt" else None
native = pytest.mark.skipif(POWERSHELL is None, reason="Native Windows PowerShell is required")


def test_default_report_requires_explicit_apply_and_preserves_backup():
    text = SCRIPT.read_text()
    assert "[switch] $Apply" in text and "if ($Apply)" in text
    assert "SupportsShouldProcess = $true" in text
    assert "$PSCmdlet.ShouldProcess(" in text
    assert "22621" in text and "$os.ProductType -eq 1" in text
    assert "[IO.File]::Replace($temp, $target, $backup)" in text
    assert "[IO.File]::Move($temp, $target)" in text


def test_shutdown_and_firewall_commands_are_only_printed_guidance():
    text = SCRIPT.read_text()
    for line in text.splitlines():
        if re.search(r"wsl\s+--shutdown|New-NetFirewallHyperVRule", line):
            assert line.startswith("Write-Host "), line
    assert "Set-NetFirewallHyperVVMSetting" not in text
    assert "-DefaultInboundAction" not in text
    assert "-LocalPorts ' + $TcpPort" in text
    assert "listenAddresses" in text


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def invoke(config, *args, build=22631):
    # No production-host compatibility bypass is added to the helper itself.
    # Mock the read-only CIM lookup in this disposable PowerShell process.
    command = f"""
$ErrorActionPreference = 'Stop'
function Get-CimInstance {{
    param([string] $ClassName)
    [pscustomobject]@{{ BuildNumber = '{build}'; ProductType = 1 }}
}}
function wsl {{ throw 'Unexpected WSL command' }}
function wsl.exe {{ throw 'Unexpected WSL command' }}
function New-NetFirewallHyperVRule {{ throw 'Unexpected firewall mutation' }}
& {quote(SCRIPT)} -ConfigPath {quote(config)} {' '.join(args)}
"""
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-Command", command], capture_output=True, text=True, timeout=15)


@native
def test_report_and_whatif_leave_configuration_and_directory_unchanged(tmp_path):
    config = tmp_path / ".wslconfig"
    original = b"[wsl2]\r\nmemory=4GB\r\nnetworkingMode=nat\r\n"
    config.write_bytes(original)
    report = invoke(config)
    assert report.returncode == 0, report.stderr
    assert "22631" in report.stdout and "nat" in report.stdout
    preview = invoke(config, "-Apply", "-WhatIf")
    assert preview.returncode == 0, preview.stderr
    assert config.read_bytes() == original
    assert set(tmp_path.iterdir()) == {config}


@native
@pytest.mark.parametrize("encoding,bom", [("utf-8", b""), ("utf-16-le", b"\xff\xfe")])
def test_apply_preserves_encoding_comments_other_sections_and_exact_backup(tmp_path, encoding, bom):
    config = tmp_path / ".wslconfig"
    original_text = "# retained\r\n[wsl2]\r\nmemory=6GB\r\n  networkingMode = nat  # selected\r\n[experimental]\r\nsparseVhd=true\r\n"
    original = bom + original_text.encode(encoding)
    config.write_bytes(original)
    applied = invoke(config, "-Apply", "-TcpPort", "4101")
    assert applied.returncode == 0, applied.stderr
    assert config.read_bytes() == bom + original_text.replace("= nat", "= mirrored").encode(encoding)
    backups = list(tmp_path.glob(".wslconfig.syn-backup-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert '/tcp/4101' in applied.stdout and "-LocalPorts 4101" in applied.stdout
    again = invoke(config, "-Apply")
    assert again.returncode == 0, again.stderr
    assert list(tmp_path.glob(".wslconfig.syn-backup-*")) == backups
    assert not list(tmp_path.glob("*.tmp"))


@native
@pytest.mark.parametrize("original,expected", [
    ("[wsl2]\nmemory=6GB\n[experimental]\nsparseVhd=true\n",
     "[wsl2]\nmemory=6GB\nnetworkingMode=mirrored\n[experimental]\nsparseVhd=true\n"),
    ("[experimental]\nsparseVhd=true", "[experimental]\nsparseVhd=true\n[wsl2]\nnetworkingMode=mirrored\n"),
])
def test_missing_setting_or_section_preserves_unrelated_lines(tmp_path, original, expected):
    config = tmp_path / ".wslconfig"
    config.write_bytes(original.encode())
    applied = invoke(config, "-Apply")
    assert applied.returncode == 0, applied.stderr
    assert config.read_bytes() == expected.encode()


@native
def test_unsupported_windows_build_does_not_modify_configuration(tmp_path):
    config = tmp_path / ".wslconfig"
    original = b"[wsl2]\nnetworkingMode=nat\n"
    config.write_bytes(original)
    report = invoke(config, build=19045)
    assert report.returncode == 0, report.stderr
    assert "19045" in report.stdout
    applied = invoke(config, "-Apply", build=19045)
    assert applied.returncode != 0
    assert "Windows 11 22H2" in applied.stderr
    assert config.read_bytes() == original
    assert set(tmp_path.iterdir()) == {config}


@native
@pytest.mark.parametrize("original", [
    "[wsl2]\nnetworkingMode=nat\nnetworkingMode=none\n",
    "[wsl2]\nmemory=4GB\n[wsl2]\nnetworkingMode=nat\n",
])
def test_ambiguous_config_is_rejected_without_any_write(tmp_path, original):
    config = tmp_path / ".wslconfig"
    config.write_bytes(original.encode())
    result = invoke(config, "-Apply")
    assert result.returncode != 0
    assert "ambiguous" in result.stderr
    assert config.read_bytes() == original.encode()
    assert set(tmp_path.iterdir()) == {config}


@native
def test_first_config_is_created_only_on_explicit_apply(tmp_path):
    config = tmp_path / ".wslconfig"
    report = invoke(config)
    assert report.returncode == 0, report.stderr
    assert not config.exists()
    applied = invoke(config, "-Apply")
    assert applied.returncode == 0, applied.stderr
    assert config.read_bytes() == b"[wsl2]\r\nnetworkingMode=mirrored\r\n"
    assert set(tmp_path.iterdir()) == {config}
