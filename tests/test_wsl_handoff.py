"""Windows-to-WSL handoff never executes the inherited Windows uv binary."""
from __future__ import annotations

from types import SimpleNamespace

from syn.startup import _WSL_UV_COMMAND, handoff_windows


def test_windows_handoff_uses_checkout_bootstrap(monkeypatch, tmp_path):
    monkeypatch.setattr("syn.startup.platform.system", lambda: "Windows")
    monkeypatch.setattr("syn.startup.shutil.which", lambda _: r"C:\Windows\System32\wsl.exe")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "bootstrap-wsl.sh").write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.chdir(tmp_path)
    invocations = []

    def fake_run(argv, **kwargs):
        invocations.append((argv, kwargs))
        if argv[2] == "wslpath":
            return SimpleNamespace(stdout="/mnt/c/source/Syn\n", returncode=0)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("syn.startup.subprocess.run", fake_run)
    options = SimpleNamespace(
        api_only=False, config=tmp_path / "config.toml",
        host="127.0.0.1", port=8000, logLevel="info", operator_config=None,
    )
    assert handoff_windows(options) is True
    argv, kwargs = invocations[-1]
    assert argv[1:4] == ["--exec", "sh", "-c"]
    assert argv[4] == _WSL_UV_COMMAND
    assert argv[5] == "syn-wsl"
    assert argv[8] == "syn-server"  # argv[7] is the translated bootstrap path
    assert kwargs == {"check": False}
    assert '$PATH' not in _WSL_UV_COMMAND
    assert 'exec sh "$bootstrap"' in _WSL_UV_COMMAND


def test_api_only_never_starts_wsl(monkeypatch):
    monkeypatch.setattr("syn.startup.platform.system", lambda: "Windows")
    monkeypatch.setattr("syn.startup.shutil.which", lambda _: (_ for _ in ()).throw(
        AssertionError("WSL lookup should not run")))
    assert handoff_windows(SimpleNamespace(api_only=True)) is False


def test_windows_missing_checkout_script_rejected(monkeypatch, tmp_path):
    import pytest

    monkeypatch.setattr("syn.startup.platform.system", lambda: "Windows")
    monkeypatch.setattr("syn.startup.shutil.which", lambda _: r"C:\Windows\System32\wsl.exe")
    monkeypatch.chdir(tmp_path)
    # Ensure fallback to the actual repository source is disabled for this test.
    from syn import startup

    monkeypatch.setattr(startup, "__file__", str(tmp_path / "src" / "syn" / "startup.py"))
    with pytest.raises(RuntimeError, match="bootstrap script is missing"):
        handoff_windows(SimpleNamespace(
            api_only=False, config=tmp_path / "config.toml",
            host="127.0.0.1", port=8000, logLevel="info", operator_config=None,
        ))


def test_windows_first_run_does_not_force_windows_config_location(monkeypatch, tmp_path):
    """The bootstrap chooses a private native Linux config on first run."""
    from pathlib import Path

    monkeypatch.setattr("syn.startup.platform.system", lambda: "Windows")
    monkeypatch.setattr("syn.startup.shutil.which", lambda _: r"C:\Windows\System32\wsl.exe")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "bootstrap-wsl.sh").write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.chdir(tmp_path)
    invocations = []

    def fake_run(argv, **kwargs):
        invocations.append(argv)
        if argv[2] == "wslpath":
            return SimpleNamespace(stdout="/mnt/c/source/Syn\n", returncode=0)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("syn.startup.subprocess.run", fake_run)
    options = SimpleNamespace(
        api_only=False, config=Path("config.toml"), host="127.0.0.1",
        port=8000, logLevel="info", operator_config=None,
    )
    assert handoff_windows(options) is True
    assert "--config" not in invocations[-1]
