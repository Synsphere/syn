from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path

import pytest

from syn import DatabaseConfig, P2PConfig, SynConfig, loadConfig


def writeConfig(tmpPath: Path, body: str) -> Path:
    path = tmpPath / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_config_with_defaults(tmp_path: Path) -> None:
    path = writeConfig(tmp_path, '[database]\nurl = "postgresql+psycopg://localhost/syn"\n')
    config = loadConfig(path)
    assert config == SynConfig(
        database=DatabaseConfig(
            url="postgresql+psycopg://localhost/syn", echo=False, autoMigrate=True
        )
    )


def test_load_config_with_echo_enabled(tmp_path: Path) -> None:
    path = writeConfig(
        tmp_path,
        '[database]\nurl = "postgresql+psycopg://localhost/syn"\necho = true\n',
    )
    assert loadConfig(path).database.echo is True


def test_load_config_can_disable_automatic_migrations(tmp_path: Path) -> None:
    path = writeConfig(
        tmp_path,
        '[database]\nurl = "postgresql+psycopg://localhost/syn"\nautoMigrate = false\n',
    )
    assert loadConfig(path).database.autoMigrate is False


def test_load_config_reads_p2p_settings(tmp_path: Path) -> None:
    path = writeConfig(
        tmp_path,
        '[database]\nurl = "postgresql+psycopg://localhost/syn"\n'
        '[p2p]\nenabled = true\nmdnsEnabled = false\nupnpEnabled = false\n'
        'listenAddresses = ["/ip4/0.0.0.0/tcp/4001"]\n'
        'bootstrapPeers = ["/ip4/127.0.0.1/tcp/4002/p2p/QmPeer"]\n'
        'startupTimeoutSeconds = 5\n',
    )

    assert loadConfig(path).p2p == P2PConfig(
        enabled=True,
        mdnsEnabled=False,
        upnpEnabled=False,
        listenAddresses=("/ip4/0.0.0.0/tcp/4001",),
        bootstrapPeers=("/ip4/127.0.0.1/tcp/4002/p2p/QmPeer",),
        startupTimeoutSeconds=5.0,
    )


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("enabled", '"yes"', "p2p.enabled"),
        ("mdnsEnabled", '"yes"', "p2p.mdnsEnabled"),
        ("upnpEnabled", '"yes"', "p2p.upnpEnabled"),
        ("listenAddresses", "[]", "p2p.listenAddresses"),
        ("bootstrapPeers", '[1]', "p2p.bootstrapPeers"),
        ("startupTimeoutSeconds", "0", "p2p.startupTimeoutSeconds"),
    ],
)
def test_invalid_p2p_settings_are_rejected(
    tmp_path: Path, setting: str, value: str, message: str
) -> None:
    body = (
        '[database]\nurl = "postgresql+psycopg://localhost/syn"\n'
        f"[p2p]\n{setting} = {value}\n"
    )
    with pytest.raises(ValueError, match=message):
        loadConfig(writeConfig(tmp_path, body))


def test_config_objects_are_immutable(tmp_path: Path) -> None:
    path = writeConfig(tmp_path, '[database]\nurl = "postgresql+psycopg://localhost/syn"\n')
    config = loadConfig(path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.database.url = "changed"  # type: ignore[misc]


def test_missing_config_file_propagates_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        loadConfig(tmp_path / "missing.toml")


@pytest.mark.parametrize(
    "body",
    [
        "",
        "title = 'missing database table'\n",
        "[database]\n",
        "[database]\necho = false\n",
        "database = 'wrong type'\n",
    ],
)
def test_missing_database_url_is_rejected(tmp_path: Path, body: str) -> None:
    with pytest.raises(ValueError, match="database.url"):
        loadConfig(writeConfig(tmp_path, body))


@pytest.mark.parametrize("value", ["''", "1", "true", "[]", "{}"])
def test_invalid_database_url_is_rejected(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        loadConfig(writeConfig(tmp_path, f"[database]\nurl = {value}\n"))


@pytest.mark.parametrize("value", ["'yes'", "1", "[]", "{}"])
def test_non_boolean_echo_is_rejected(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="true or false"):
        loadConfig(
            writeConfig(
                tmp_path,
                f'[database]\nurl = "postgresql+psycopg://localhost/syn"\necho = {value}\n',
            )
        )


@pytest.mark.parametrize("value", ["'yes'", "1", "[]", "{}"])
def test_non_boolean_auto_migrate_is_rejected(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="database.autoMigrate must be true or false"):
        loadConfig(
            writeConfig(
                tmp_path,
                f'[database]\nurl = "postgresql+psycopg://localhost/syn"\n'
                f"autoMigrate = {value}\n",
            )
        )


def test_malformed_toml_is_reported(tmp_path: Path) -> None:
    with pytest.raises(tomllib.TOMLDecodeError):
        loadConfig(writeConfig(tmp_path, "[database\nurl = 'broken'\n"))
