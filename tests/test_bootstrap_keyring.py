"""Headless Secret Service setup must not leak/replace identity secrets."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def keyring_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap-keyring.py"
    spec = importlib.util.spec_from_file_location("bootstrap_keyring_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_bus_refuses_unlock(keyring_module, monkeypatch):
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    with pytest.raises(RuntimeError, match="D-Bus"):
        keyring_module.unlock()


def test_existing_service_requires_no_new_password(keyring_module, monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "test")
    monkeypatch.setattr(keyring_module, "service_running", lambda: True)
    monkeypatch.setattr(keyring_module, "getpass", lambda *_: (_ for _ in ()).throw(
        AssertionError("Never prompt for an existing unlocked service")))
    keyring_module.unlock()


def test_fresh_service_encrypts_with_explicit_confirmed_passphrase(
    keyring_module, monkeypatch, tmp_path
):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "test")
    checks = iter((False, True))
    monkeypatch.setattr(keyring_module, "service_running", lambda: next(checks))
    monkeypatch.setattr(keyring_module.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(keyring_module.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    prompts = iter(("long-encrypted-passphrase", "long-encrypted-passphrase"))
    monkeypatch.setattr(keyring_module, "getpass", lambda *_: next(prompts))
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(keyring_module.subprocess, "run", fake_run)
    keyring_module.unlock()
    assert commands[0][0] == ["gnome-keyring-daemon", "--unlock", "--components=secrets"]
    assert commands[0][1]["input"] == "long-encrypted-passphrase\n"
    assert commands[0][1]["capture_output"] is True


def test_short_or_unconfirmed_passphrase_cannot_create_keyring(keyring_module, monkeypatch, tmp_path):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "test")
    monkeypatch.setattr(keyring_module, "service_running", lambda: False)
    monkeypatch.setattr(keyring_module.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(keyring_module.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(keyring_module, "getpass", lambda *_: "short")
    monkeypatch.setattr(keyring_module.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("No daemon spawn for unsafe passphrases")))
    with pytest.raises(RuntimeError, match="12 characters"):
        keyring_module.unlock()


def test_headless_first_setup_fails_before_touching_keyring(keyring_module, monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "test")
    monkeypatch.setattr(keyring_module, "service_running", lambda: False)
    monkeypatch.setattr(keyring_module.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(RuntimeError, match="terminal"):
        keyring_module.unlock()
