"""Regression checks for Windows-to-WSL operator handoff.

The WSL shell must never resolve the Windows uv executable from WSLENV/PATH.
"""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from syn.startup import _WSL_UV_COMMAND, handoff_windows


def test_windows_handoff_uses_linux_only_uv_path(monkeypatch, tmp_path):
    monkeypatch.setattr("syn.startup.platform.system", lambda: "Windows")
    monkeypatch.setattr("syn.startup.shutil.which", lambda _: r"C:\Windows\System32\wsl.exe")
    invocations = []

    def fake_run(argv, **kwargs):
        invocations.append((argv, kwargs))
        if argv[2] == "wslpath":
            return SimpleNamespace(stdout="/mnt/c/source/Syn\n", returncode=0)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("syn.startup.subprocess.run", fake_run)
    options = SimpleNamespace(api_only=False, config=tmp_path / "config.toml",
                              host="127.0.0.1", port=8000, logLevel="info",
                              operator_config=None)
    assert handoff_windows(options) is True
    argv, kwargs = invocations[-1]
    assert argv[1:4] == ["--exec", "sh", "-c"]
    assert argv[4] == _WSL_UV_COMMAND
    assert argv[5] == "syn-wsl"
    assert argv[7] == "syn-server"
    assert kwargs == {"check": False}
    assert '$PATH' not in _WSL_UV_COMMAND  # Does not inherit a Windows executable.
    assert 'UV_PROJECT_ENVIRONMENT=.venv-wsl' in _WSL_UV_COMMAND


def test_api_only_never_starts_wsl(monkeypatch):
    monkeypatch.setattr("syn.startup.platform.system", lambda: "Windows")
    monkeypatch.setattr("syn.startup.shutil.which", lambda _: (_ for _ in ()).throw(
        AssertionError("WSL lookup should not run")))
    assert handoff_windows(SimpleNamespace(api_only=True)) is False


@pytest.mark.skipif(os.name == "nt", reason="Requires a POSIX sh for WSL-script simulation")
def test_linux_uv_selected_not_inherited_windows_path(tmp_path):
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    uv = local / "uv"
    uv.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then exit 0; fi\nprintf '%s\\n' \"$@\" > \"$HOME/uv_args\"\n")
    uv.chmod(0o700)
    fake_windows = tmp_path / "windows" / "uv"
    fake_windows.parent.mkdir()
    fake_windows.write_text("#!/bin/sh\necho BAD-WINDOWS-UV >&2\nexit 1\n")
    fake_windows.chmod(0o700)
    env = dict(os.environ, HOME=str(tmp_path), PATH=f"{fake_windows.parent}:/usr/bin:/bin")
    result = subprocess.run(["sh", "-c", _WSL_UV_COMMAND,
                             "syn-wsl", str(tmp_path), "syn-server", "--api-only"],
                            env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "uv_args").read_text().splitlines() == [
        "run", "syn-server", "--api-only"]
    assert "BAD-WINDOWS-UV" not in result.stderr
