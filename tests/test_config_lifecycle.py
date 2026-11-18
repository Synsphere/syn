"""Preservation and recovery of existing Syn bootstrap configs (no live services)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from syn.config_lifecycle import (
    ConfigRecoveryError, configuration_report, remember_good_config,
    restore_good_config,
)


URL = "postgresql+psycopg://syn:sensitive-password@localhost/syn"


def config_file(tmp_path: Path, *, p2p: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'[database]\nurl = "{URL}"\n{p2p}', encoding="utf-8")
    return path


def test_legacy_config_inherits_default_on_capabilities_without_rewriting(tmp_path):
    path = config_file(tmp_path)
    original = path.read_bytes()
    result = configuration_report(path)
    assert result["configuration"] == "valid"
    assert all(result["p2p"][name] for name in (
        "amino", "dns4", "mdns", "quic", "upnp", "webrtc", "webtransport", "websocket"
    ))
    assert result["services"]["database"] == "configured-not-probed"
    assert path.read_bytes() == original
    assert "sensitive-password" not in json.dumps(result)
    assert "localhost" not in json.dumps(result)


def test_explicit_opt_out_survives_upgrade(tmp_path):
    path = config_file(tmp_path, p2p="[p2p]\nwebRtcEnabled = false\nquicEnabled = false\n")
    original = path.read_bytes()
    result = configuration_report(path)
    assert result["p2p"]["webrtc"] is False
    assert result["p2p"]["quic"] is False
    assert result["p2p"]["webtransport"] is True
    assert path.read_bytes() == original


def test_snapshot_private_repeatable_and_does_not_modify_user_config(tmp_path):
    path = config_file(tmp_path)
    original = path.read_bytes()
    state = tmp_path / "state"
    backup = remember_good_config(path, state_root=state)
    assert backup.read_bytes() == original
    assert path.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700
    mtime = backup.stat().st_mtime_ns
    assert remember_good_config(path, state_root=state) == backup
    assert backup.stat().st_mtime_ns == mtime
    # A valid user change replaces the owner-only snapshot, not the user file.
    path.write_text(path.read_text() + "\n[p2p]\nquicEnabled = false\n")
    new_bytes = path.read_bytes()
    remember_good_config(path, state_root=state)
    assert backup.read_bytes() == new_bytes
    assert path.read_bytes() == new_bytes


def test_malformed_config_does_not_destroy_known_good(tmp_path):
    path = config_file(tmp_path)
    state = tmp_path / "state"
    backup = remember_good_config(path, state_root=state)
    original = backup.read_bytes()
    broken = b"[database\nurl = garbage\n"
    path.write_bytes(broken)
    with pytest.raises(ValueError, match="Invalid Syn TOML"):
        remember_good_config(path, state_root=state)
    assert path.read_bytes() == broken
    assert backup.read_bytes() == original


def test_explicit_restore_preserves_bad_file_and_repairs_atomically(tmp_path):
    path = config_file(tmp_path)
    state = tmp_path / "state"
    remember_good_config(path, state_root=state)
    original = path.read_bytes()
    damaged = b"[database\nurl = malformed\n"
    path.write_bytes(damaged)
    preserved = restore_good_config(path, state_root=state)
    assert path.read_bytes() == original
    assert preserved.read_bytes() == damaged
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(preserved.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".syn-config-*.tmp"))
    with pytest.raises(ConfigRecoveryError, match="valid"):
        restore_good_config(path, state_root=state)


def test_bad_or_missing_snapshot_is_not_restored(tmp_path):
    path = config_file(tmp_path)
    state = tmp_path / "state"
    path.write_text("[database\n")
    with pytest.raises(ConfigRecoveryError, match="No validated"):
        restore_good_config(path, state_root=state)
    backup_path = state / __import__('hashlib').sha256(os.fsencode(str(path.absolute()))).hexdigest() / "last-good.toml"
    backup_path.write_text("[database\n")
    with pytest.raises(ConfigRecoveryError, match="No validated"):
        restore_good_config(path, state_root=state)
    assert path.read_text() == "[database\n"


def test_symlink_and_hardlink_configs_are_never_modified(tmp_path):
    original = config_file(tmp_path)
    state = tmp_path / "state"
    linked = tmp_path / "linked.toml"
    linked.symlink_to(original)
    with pytest.raises(OSError):
        remember_good_config(linked, state_root=state)
    with pytest.raises(OSError):
        restore_good_config(linked, state_root=state)
    hardlink = tmp_path / "hardlink.toml"
    os.link(original, hardlink)
    with pytest.raises(ConfigRecoveryError, match="regular file"):
        remember_good_config(original, state_root=state)


def test_server_check_does_not_start_operator_or_touch_config(tmp_path, capsys, monkeypatch):
    from syn.server import main
    from syn import startup

    path = config_file(tmp_path)
    original = path.read_bytes()
    monkeypatch.setattr(startup, "ensure_operator", lambda *a: pytest.fail("operator must not start"))
    monkeypatch.setattr(startup, "ensure_database_config", lambda *a: pytest.fail("no auto-create"))
    main(["--check-config", "--config", str(path)])
    stdout = capsys.readouterr().out
    assert json.loads(stdout)["p2p"]["webrtc"]
    assert "sensitive-password" not in stdout
    assert path.read_bytes() == original


def test_server_restore_runs_without_operator(tmp_path, capsys, monkeypatch):
    from syn.server import main
    from syn import startup

    path = config_file(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    remember_good_config(path)
    original = path.read_bytes()
    path.write_text("[database\n")
    monkeypatch.setattr(startup, "ensure_operator", lambda *a: pytest.fail("operator must not start"))
    main(["--restore-config", "--config", str(path)])
    output = capsys.readouterr()
    assert "Restored last-known-good" in output.err
    assert json.loads(output.out)["configuration"] == "valid"
    assert path.read_bytes() == original


def test_server_invalid_config_prevents_operator_provisioning(tmp_path, monkeypatch):
    from syn.server import main
    from syn import startup

    path = config_file(tmp_path)
    path.write_text("[database\n")
    monkeypatch.setattr(startup, "ensure_operator", lambda *a: pytest.fail("operator must not start"))
    with pytest.raises(ValueError, match="Invalid Syn TOML"):
        main(["--config", str(path)])
    assert path.read_text() == "[database\n"
