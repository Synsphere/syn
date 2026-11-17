"""Windows starts its host custody broker before opening the WSL operator."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from syn.startup import handoff_windows


def test_windows_handoff_delegates_to_host_launcher(monkeypatch, tmp_path):
    from syn import startup
    monkeypatch.setattr(startup.platform, "system", lambda: "Windows")
    monkeypatch.setattr(startup.shutil, "which", lambda name: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    launcher = tmp_path / "Start-Syn.ps1"
    launcher.write_text("exit 0\n")
    monkeypatch.setattr(startup, "__file__", str(tmp_path / "src" / "syn" / "startup.py"))
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(startup.subprocess, "run", fake_run)
    options = SimpleNamespace(api_only=False, config=Path("config.toml"), host="127.0.0.1",
                              port=8000, logLevel="info", operator_config=None)
    assert handoff_windows(options) is True
    argv, kwargs = calls[-1]
    assert "Start-Syn.ps1" in " ".join(argv)
    assert argv[argv.index("-BindHost") + 1] == "127.0.0.1"
    assert argv[argv.index("-BindPort") + 1] == "8000"
    assert "-Config" not in argv  # Default allows Linux-private config creation.
    assert kwargs == {"check": False}


def test_api_only_never_starts_host_broker(monkeypatch):
    from syn import startup
    monkeypatch.setattr(startup.platform, "system", lambda: "Windows")
    monkeypatch.setattr(startup.shutil, "which", lambda _: (_ for _ in ()).throw(
        AssertionError("Must not locate host custody in API-only mode")))
    assert handoff_windows(SimpleNamespace(api_only=True)) is False


def test_missing_host_launcher_fails_closed(monkeypatch, tmp_path):
    from syn import startup
    monkeypatch.setattr(startup.platform, "system", lambda: "Windows")
    monkeypatch.setattr(startup.shutil, "which", lambda _: "powershell.exe")
    monkeypatch.setattr(startup, "__file__", str(tmp_path / "src" / "syn" / "startup.py"))
    with pytest.raises(RuntimeError, match="complete checkout"):
        handoff_windows(SimpleNamespace(api_only=False))
