"""Exclusive Windows vault ownership through a mocked Win32 boundary."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host-custody"))
from syn_host_custody import broker


class Win32Error(Exception):
    def __init__(self, code, operation="CreateFile"):
        self.winerror = code
        super().__init__(code, operation, "Synthetic native error")


@pytest.fixture
def native(monkeypatch, tmp_path):
    state = SimpleNamespace(created=[], closed=[], create_error=None, close_error=None)

    def create(*arguments):
        state.created.append(arguments)
        if state.create_error is not None:
            raise state.create_error
        return object()

    def close(handle):
        state.closed.append(handle)
        if state.close_error is not None:
            error, state.close_error = state.close_error, None
            raise error

    monkeypatch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=Win32Error))
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(
        GENERIC_READ=0x80000000, GENERIC_WRITE=0x40000000,
        OPEN_ALWAYS=4, FILE_ATTRIBUTE_NORMAL=128))
    monkeypatch.setitem(sys.modules, "win32file", SimpleNamespace(CreateFile=create, CloseHandle=close))
    monkeypatch.setitem(sys.modules, "win32crypt", SimpleNamespace())
    # Replace only this module's reference; mutating the real os.name would make
    # pathlib/pytest try to instantiate WindowsPath on a Linux test host.
    monkeypatch.setattr(broker, "os", SimpleNamespace(
        **{**vars(os), "name": "nt", "environ": {"LOCALAPPDATA": str(tmp_path)}}))
    return state


def test_vault_uses_exclusive_existing_lock_and_close_is_idempotent(native, tmp_path):
    lock = tmp_path / "authority.lock"
    lock.write_text("retained existing lock file")
    vault = broker.WindowsVault(tmp_path)
    handle = vault._lock
    arguments = native.created[0]
    assert arguments == (str(lock), 0xC0000000, 0, None, 4, 128, None)
    assert vault.data == {"version": 1, "identities": {}, "slots": {}, "approvals": []}
    vault.close()
    vault.close()
    assert native.closed == [handle]
    assert lock.read_text() == "retained existing lock file"
    assert not vault.path.exists()


@pytest.mark.parametrize("code", [32, 33])
def test_contention_has_actionable_error_without_loading_or_changing_vault(native, monkeypatch, tmp_path, code):
    sealed = tmp_path / "vault-v1.dpapi"
    sealed.write_bytes(b"existing sealed identity")
    native.create_error = Win32Error(code)
    monkeypatch.setattr(broker.WindowsVault, "_load", lambda _: pytest.fail("must not load a busy vault"))
    with pytest.raises(broker.VaultInUseError, match="Another Syn host-custody process") as caught:
        broker.WindowsVault(tmp_path)
    assert caught.value.__cause__ is native.create_error
    assert caught.value.lock_path == tmp_path / "authority.lock"
    assert "Do not delete authority.lock or vault-v1.dpapi" in str(caught.value)
    assert native.closed == []
    assert sealed.read_bytes() == b"existing sealed identity"


def test_access_denied_is_not_misreported_as_an_existing_broker(native, tmp_path):
    native.create_error = Win32Error(5)
    with pytest.raises(Win32Error) as caught:
        broker.WindowsVault(tmp_path)
    assert caught.value is native.create_error
    assert native.closed == []


@pytest.mark.parametrize("error", [ValueError("Corrupt vault"), KeyboardInterrupt()])
def test_failed_load_releases_its_exclusive_handle(native, monkeypatch, tmp_path, error):
    def fail(_):
        raise error
    monkeypatch.setattr(broker.WindowsVault, "_load", fail)
    with pytest.raises(type(error)) as caught:
        broker.WindowsVault(tmp_path)
    assert caught.value is error
    assert len(native.closed) == 1


def test_context_manager_closes_after_exception(native, tmp_path):
    with pytest.raises(ValueError, match="request failed"):
        with broker.WindowsVault(tmp_path) as vault:
            assert not vault.has_identity("syn.operator/test")
            raise ValueError("request failed")
    assert len(native.closed) == 1
    assert vault._lock is None


def test_failed_close_retains_handle_for_retry_and_fences_all_access(native, tmp_path):
    vault = broker.WindowsVault(tmp_path)
    handle = vault._lock
    native.close_error = Win32Error(6, "CloseHandle")
    with pytest.raises(Win32Error):
        vault.close()
    assert vault._lock is handle
    with pytest.raises(OSError, match="closed"):
        vault.has_identity("syn.operator/test")
    vault.close()
    assert native.closed == [handle, handle]
    assert vault._lock is None


@pytest.mark.parametrize("operation", [
    lambda vault: vault.has_identity("syn.operator/test"),
    lambda vault: vault.create_identity("syn.operator/test", b"x" * 32, {}),
    lambda vault: vault.identity("syn.operator/test"),
    lambda vault: vault.get_slot("syn.operator", "test"),
    lambda vault: vault.set_slot("syn.operator", "test", "value"),
    lambda vault: vault.is_approved("syn.operator/test", "syn", "group", 1),
    lambda vault: vault.approve_group("syn.operator/test", "syn", "group", 1),
])
def test_closed_vault_cannot_read_or_mutate_authority(native, tmp_path, operation):
    vault = broker.WindowsVault(tmp_path)
    vault.close()
    with pytest.raises(OSError, match="closed"):
        operation(vault)
    assert not vault.path.exists()


def test_default_directory_is_existing_per_user_host_custody_location(native, tmp_path):
    with broker.WindowsVault() as vault:
        assert vault.path == tmp_path / "Syn" / "HostCustody" / "vault-v1.dpapi"
        assert vault.lock_path == tmp_path / "Syn" / "HostCustody" / "authority.lock"
