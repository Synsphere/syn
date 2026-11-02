"""The public package probe must not start services or consume private config."""
from importlib.metadata import version
import json
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from syn_client.releases import RuntimeContract

from syn.api import createApp
from syn.config import P2PConfig
from syn.release import runtimeContract
from syn.server import main


def test_installed_package_contract_has_current_profiles_and_no_node_activation():
    contract = runtimeContract()
    assert contract.runtime_version == version("syn")
    assert contract.client_version == version("syn-client")
    assert contract.node_modes == ("agent", "operator", "service")
    assert "syn.operator-local/1" in contract.api_profiles
    assert "syn.agent-local/1" in contract.api_profiles
    assert "encrypted-private-pubsub" not in contract.capabilities
    assert "live-consensus" not in contract.capabilities
    assert "syn.dag-hashgraph-finality/2" in contract.api_profiles
    assert RuntimeContract.from_dict(contract.to_dict()) == contract


def test_cli_probe_does_not_read_config_migrate_load_tokens_or_start_network(capsys):
    with (
        patch("syn.server.loadConfig", side_effect=AssertionError("private config read")),
        patch("syn.server.createEngine", side_effect=AssertionError("database access")),
        patch("syn.server.createApp", side_effect=AssertionError("application/network startup")),
        patch("syn.server.uvicorn.run", side_effect=AssertionError("server startup")),
    ):
        main(["--describe-json", "--config", "nonexistent-private-config.toml"])
    raw = capsys.readouterr().out
    assert RuntimeContract.from_dict(json.loads(raw)) == runtimeContract()
    assert "nonexistent-private-config" not in raw


def test_capabilities_route_describes_installed_code_not_enabled_secret_features():
    sessions = Mock(side_effect=AssertionError("database access"))
    app = createApp(sessions, {}, P2PConfig(enabled=False), communicationTokens={})
    with TestClient(app) as client:
        response = client.get("/v1/capabilities")
        assert response.status_code == 200
        assert RuntimeContract.from_dict(response.json()) == runtimeContract()
        assert not any(p.startswith("/v1/communication/") for p in client.get("/openapi.json").json()["paths"])
    sessions.assert_not_called()


def test_contract_database_revision_is_the_packaged_migration_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from pathlib import Path
    import syn

    config = Config()
    config.set_main_option("script_location", str(Path(syn.__file__).parent / "alembic"))
    assert ScriptDirectory.from_config(config).get_current_head() == runtimeContract().database_revision
