"""Use disposable directories; never touch the runner's actual system bus."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat

import pytest


pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux D-Bus runtime modes")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/repair-system-dbus.py"


@pytest.fixture
def repair():
    specification = importlib.util.spec_from_file_location("syn_test_dbus_repair", SCRIPT)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def owner():
    return {"owner_uid": os.getuid(), "owner_gid": os.getgid()}


def test_only_existing_private_runtime_directory_is_repaired(repair, tmp_path):
    directory = tmp_path / "bus"
    assert repair.repair_directory(directory, **owner()) == "absent"
    assert not directory.exists()
    directory.mkdir(mode=0o700)
    private = directory / "private-control"
    private.write_text("unchanged")
    private.chmod(0o600)
    assert repair.repair_directory(directory, **owner()) == "repaired"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert private.read_text() == "unchanged"
    assert repair.repair_directory(directory, **owner()) == "unchanged"


@pytest.mark.parametrize("mode", [0o750, 0o711, 0o770, 0o2770])
def test_custom_permissions_are_never_changed(repair, tmp_path, mode):
    directory = tmp_path / "bus"
    directory.mkdir()
    directory.chmod(mode)
    with pytest.raises(repair.RepairRefused, match="not the known"):
        repair.repair_directory(directory, **owner())
    assert stat.S_IMODE(directory.stat().st_mode) == mode


@pytest.mark.parametrize("field", ["owner_uid", "owner_gid"])
def test_foreign_owner_or_group_is_not_repaired(repair, tmp_path, field):
    directory = tmp_path / "bus"
    directory.mkdir(mode=0o700)
    expected = owner()
    expected[field] += 1
    with pytest.raises(repair.RepairRefused, match="owner/group"):
        repair.repair_directory(directory, **expected)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.parametrize("acl", ["system.posix_acl_access", "system.posix_acl_default"])
def test_extended_acls_are_never_expanded(repair, tmp_path, monkeypatch, acl):
    directory = tmp_path / "bus"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(repair.os, "listxattr", lambda descriptor: [acl])
    with pytest.raises(repair.RepairRefused, match="ACL"):
        repair.repair_directory(directory, **owner())
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_symlink_is_not_followed(repair, tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "bus"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        repair.repair_directory(link, **owner())
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_changed_directory_cannot_redirect_the_repair(repair, tmp_path, monkeypatch):
    directory = tmp_path / "bus"
    original = tmp_path / "original"
    directory.mkdir(mode=0o700)

    def rename_during_inspection(descriptor):
        directory.rename(original)
        directory.mkdir(mode=0o700)
        return []

    monkeypatch.setattr(repair.os, "listxattr", rename_during_inspection)
    with pytest.raises(repair.RepairRefused, match="changed during inspection"):
        repair.repair_directory(directory, **owner())
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(original.stat().st_mode) == 0o700


def test_command_has_no_path_override(repair, tmp_path, capsys):
    directory = tmp_path / "do-not-touch"
    directory.mkdir(mode=0o700)
    assert repair.main([str(directory)]) == 2
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert "fixed /run/dbus only" in capsys.readouterr().err


def test_refused_repair_is_actionable_and_nonfatal(repair, monkeypatch, capsys):
    def refused(*args, **kwargs):
        raise repair.RepairRefused("an explicit filesystem ACL requires administrator review")

    monkeypatch.setattr(repair, "repair_directory", refused)
    assert repair.main([]) == 0
    output = capsys.readouterr().out
    assert "was not changed" in output and "ACL" in output
    assert "administrator" in output and "Bluetooth remains optional" in output
