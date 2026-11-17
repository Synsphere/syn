"""First-run control flow with a mocked native custody boundary (no keys created)."""
import os
from pathlib import Path
import sys
import types
from unittest.mock import Mock

import pytest
from syn import startup


@pytest.fixture
def custody(monkeypatch):
    service = types.ModuleType("syn.operator.service")
    service.initialize = Mock()
    service.load_config = Mock(return_value=({}, "hash"))
    monkeypatch.setitem(sys.modules, "syn.operator.service", service)
    monkeypatch.setattr(startup.sys.stdin, "isatty", lambda: True)
    return service


def test_missing_operator_starts_setup_and_creates_once(tmp_path, monkeypatch, custody, capsys):
    path = tmp_path/"operator"/"operator.json"
    answers = iter(["yes", "testnet"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert startup.ensure_operator(path) == path
    assert "Starting operator setup" in capsys.readouterr().out
    custody.initialize.assert_called_once()
    assert custody.initialize.call_args.kwargs["network_id"] == "testnet"
    custody.load_config.assert_called_once_with(path)


def test_existing_operator_never_reprovisions(tmp_path, monkeypatch, custody):
    path = tmp_path/"operator.json"
    path.write_text("{}")
    monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("must not prompt")))
    assert startup.ensure_operator(path) == path
    custody.initialize.assert_not_called()
    custody.load_config.assert_called_once_with(path)


def test_cancel_does_not_create_state(tmp_path, monkeypatch, custody):
    path = tmp_path/"operator"/"operator.json"
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    with pytest.raises(RuntimeError, match="cancelled"):
        startup.ensure_operator(path)
    assert not path.parent.exists()
    custody.initialize.assert_not_called()


def test_partial_provisioning_is_not_replaced(tmp_path, custody):
    (tmp_path/"initializing.json").write_text("{}")
    with pytest.raises(RuntimeError, match="incomplete"):
        startup.ensure_operator(tmp_path/"operator.json")
    custody.initialize.assert_not_called()


def test_noninteractive_first_run_fails_clearly(tmp_path, monkeypatch, custody):
    monkeypatch.setattr(startup.sys.stdin, "isatty", lambda: False)
    with pytest.raises(RuntimeError, match="interactive"):
        startup.ensure_operator(tmp_path/"operator"/"operator.json")
    custody.initialize.assert_not_called()


def test_invalid_existing_identity_is_not_replaced(tmp_path, custody):
    path = tmp_path/"operator.json"
    path.write_text("{}")
    custody.load_config.side_effect = ValueError("invalid identity")
    with pytest.raises(ValueError, match="invalid identity"):
        startup.ensure_operator(path)
    custody.initialize.assert_not_called()


def test_database_setup_uses_environment_and_private_file(tmp_path, monkeypatch):
    path = tmp_path/"config.toml"
    monkeypatch.setenv("SYN_DATABASE_URL", "postgresql+psycopg://user:pass@localhost/syn")
    startup.ensure_database_config(path)
    assert "autoMigrate = true" in path.read_text()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_database_setup_preserves_existing_config(tmp_path, monkeypatch):
    path = tmp_path/"config.toml"
    path.write_text("original")
    monkeypatch.setenv("SYN_DATABASE_URL", "postgresql://elsewhere/new")
    startup.ensure_database_config(path)
    assert path.read_text() == "original"


def test_database_setup_rejects_sqlite(tmp_path, monkeypatch):
    path = tmp_path/"config.toml"
    monkeypatch.setenv("SYN_DATABASE_URL", "sqlite:///test.db")
    with pytest.raises(ValueError, match="PostgreSQL"):
        startup.ensure_database_config(path)
    assert not path.exists()


def test_windows_api_only_does_not_handoff(monkeypatch):
    monkeypatch.setattr(startup.platform, "system", lambda: "Windows")
    assert startup.handoff_windows(types.SimpleNamespace(api_only=True)) is False


@pytest.fixture
def server_entry(monkeypatch):
    """Exercise the actual CLI main, replacing unrelated API/feed dependency trees."""
    import importlib.util
    root = Path(__file__).resolve().parents[2]
    api = types.ModuleType("syn.api")
    api.createApp = Mock()
    feed = types.ModuleType("syn.feed_jobs")
    feed.defaultJobHandlers = Mock(return_value={})
    monkeypatch.setitem(sys.modules, "syn.api", api)
    monkeypatch.setitem(sys.modules, "syn.feed_jobs", feed)
    spec = importlib.util.spec_from_file_location("syn._clock_test_entry", root/"src/syn/server.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    from syn.config import DatabaseConfig, SynConfig
    config = SynConfig(database=DatabaseConfig("postgresql+psycopg://localhost/syn"))
    monkeypatch.setattr(entry, "loadConfig", Mock(return_value=config))
    monkeypatch.setattr(entry, "createEngine", Mock())
    monkeypatch.setattr(entry, "createSessionFactory", Mock())
    monkeypatch.setattr(entry.uvicorn, "run", Mock())
    monkeypatch.setattr(startup, "handoff_windows", Mock(return_value=False))
    monkeypatch.setattr(startup, "ensure_database_config", Mock())
    monkeypatch.setattr(startup, "ensure_operator", Mock(return_value=Path("operator.json")))
    monkeypatch.setattr(startup, "ManagedOperator", Mock())
    import syn.positions.runtime as runtime
    factory = Mock()
    factory.return_value.handlers.return_value = {}
    monkeypatch.setattr(runtime, "PositionRuntime", factory)
    return entry, config, factory


def test_syn_server_defaults_to_operator_setup(server_entry):
    entry, config, runtime = server_entry
    entry.main([])
    startup.ensure_operator.assert_called_once_with(None)
    startup.ManagedOperator.assert_called_once_with(Path("operator.json"))
    operator = startup.ManagedOperator.return_value
    assert runtime.call_args.kwargs["identity"] == operator.server.expected_hash
    assert runtime.call_args.kwargs["clockConfig"] == config.clock
    assert entry.createApp.call_args.kwargs["operatorRuntime"] is operator
    entry.uvicorn.run.assert_called_once()
    entry.createEngine.return_value.dispose.assert_called_once()


def test_api_only_is_explicit_opt_out(server_entry):
    entry, _, runtime = server_entry
    entry.main(["--api-only"])
    startup.ensure_operator.assert_not_called()
    startup.ManagedOperator.assert_not_called()
    assert "identity" not in runtime.call_args.kwargs
    assert "operatorRuntime" not in entry.createApp.call_args.kwargs
