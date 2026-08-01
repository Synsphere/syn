from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from syn.config import DatabaseConfig, SynConfig
from syn.server import main, parseArguments, serverPort


def test_server_arguments_have_safe_local_defaults() -> None:
    options = parseArguments([])

    assert options.config == Path("config.toml")
    assert options.host == "127.0.0.1"
    assert options.port == 8000
    assert options.logLevel == "info"


def test_server_arguments_accept_explicit_values() -> None:
    options = parseArguments(
        [
            "--config",
            "development.toml",
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
            "--log-level",
            "debug",
        ]
    )

    assert options.config == Path("development.toml")
    assert options.host == "0.0.0.0"
    assert options.port == 8080
    assert options.logLevel == "debug"


@pytest.mark.parametrize("value", ["0", "65536", "-1"])
def test_server_port_rejects_values_outside_tcp_range(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="between 1 and 65535"):
        serverPort(value)


def test_server_port_rejects_non_integer_value() -> None:
    with pytest.raises(ValueError):
        serverPort("not-a-port")


def test_server_builds_application_and_disposes_engine() -> None:
    config = SynConfig(
        database=DatabaseConfig(
            url="postgresql+psycopg://syn@example.test/syn",
            echo=True,
            autoMigrate=False,
        )
    )
    engine = Mock()
    sessions = Mock()
    app = Mock()
    handlers = {"example": Mock()}

    with (
        patch("syn.server.loadConfig", return_value=config) as loadConfig,
        patch("syn.server.createEngine", return_value=engine) as createEngine,
        patch("syn.server.createSessionFactory", return_value=sessions) as createSessions,
        patch("syn.server.defaultJobHandlers", return_value=handlers) as buildHandlers,
        patch("syn.server.createApp", return_value=app) as createApp,
        patch("syn.server.uvicorn.run") as runServer,
    ):
        main(
            [
                "--config",
                "server.toml",
                "--host",
                "0.0.0.0",
                "--port",
                "9000",
                "--log-level",
                "warning",
            ]
        )

    loadConfig.assert_called_once_with(Path("server.toml"))
    createEngine.assert_called_once_with(
        config.database.url,
        echo=True,
        migrate=False,
    )
    createSessions.assert_called_once_with(engine)
    buildHandlers.assert_called_once_with(config.database.url)
    createApp.assert_called_once_with(sessions, handlers)
    runServer.assert_called_once_with(
        app,
        host="0.0.0.0",
        port=9000,
        log_level="warning",
    )
    engine.dispose.assert_called_once_with()


def test_server_disposes_engine_when_uvicorn_fails() -> None:
    config = SynConfig(database=DatabaseConfig(url="postgresql+psycopg://example"))
    engine = Mock()

    with (
        patch("syn.server.loadConfig", return_value=config),
        patch("syn.server.createEngine", return_value=engine),
        patch("syn.server.createSessionFactory"),
        patch("syn.server.defaultJobHandlers", return_value={}),
        patch("syn.server.createApp", return_value=Mock()),
        patch("syn.server.uvicorn.run", side_effect=RuntimeError("server failed")),
        pytest.raises(RuntimeError, match="server failed"),
    ):
        main([])

    engine.dispose.assert_called_once_with()
