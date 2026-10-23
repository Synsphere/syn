"""Durability, refusal of replacement, workspace auth and verified HTTP consumption."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from syn_client import IntegrityError, SynClient, SynError
from syn_client.consensus import PolicyError
from syn_client.policy_adoption import GenesisPolicyAdoption

from syn.communication import communicationRouter
from syn.policy_adoption import (
    PolicyAdoptionStore,
    PolicyConflict,
    PolicyNotFound,
    adoptions,
    domains,
    metadata,
    owners,
)
from syn.policy_bootstrap import main

FIXTURE = Path(__file__).parents[1] / "packages/syn-client/tests/fixtures/policy-genesis.json"


def adoption():
    return GenesisPolicyAdoption.from_json(FIXTURE.read_text(encoding="utf-8"))


def initialize(store, document):
    return store.initialize(
        document,
        expected_workspace_id=document.workspace_id,
        expected_catalog_id=document.release.catalog_id,
        expected_adoption_digest=document.digest,
    )


@pytest.fixture
def rig():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    metadata.create_all(engine)
    sessions = sessionmaker(engine)
    yield PolicyAdoptionStore(sessions), engine
    engine.dispose()


def test_genesis_survives_reopen_and_exact_retry_with_no_replacement(tmp_path):
    engine = create_engine("sqlite:///" + (tmp_path / "policy.db").as_posix())
    metadata.create_all(engine)
    document = adoption()
    initialize(PolicyAdoptionStore(sessionmaker(engine)), document)
    engine.dispose()
    engine = create_engine("sqlite:///" + (tmp_path / "policy.db").as_posix())
    try:
        store = PolicyAdoptionStore(sessionmaker(engine))
        assert (
            store.load(document.workspace_id, document.release.catalog_id, document.digest)
            == document
        )
        assert initialize(store, document) == document
        changed = replace(document, release=replace(document.release, release_revision="2"))
        with pytest.raises(PolicyConflict, match="governed transitions"):
            initialize(store, changed)
        with pytest.raises(PolicyNotFound):
            store.load(document.workspace_id, document.release.catalog_id, changed.digest)
    finally:
        engine.dispose()


def test_cross_catalog_namespace_and_genesis_conflicts_roll_back(rig):
    store, engine = rig
    document = adoption()
    initialize(store, document)
    other = replace(document, release=replace(document.release, catalog_id=document.workspace_id))
    for changed in (
        other,
        replace(other, bindings=(replace(other.bindings[0], genesis_digest="f" * 64),)),
    ):
        with pytest.raises(PolicyConflict):
            initialize(store, changed)
        with engine.connect() as connection:
            assert len(connection.execute(select(adoptions)).all()) == 1
            assert len(connection.execute(select(domains)).all()) == 1
            assert len(connection.execute(select(owners)).all()) == 1
    assert (
        store.load(document.workspace_id, document.release.catalog_id, document.digest) == document
    )
    # A separate local workspace has separate explicit trust configuration.
    initialize(store, replace(other, workspace_id=document.release.catalog_id))


def test_another_catalog_can_own_a_distinct_namespace_in_same_domain(rig):
    store, _engine = rig
    document = adoption()
    initialize(store, document)
    definition = document.release.types[0]
    policy = replace(definition.consensus_policy, conflict_namespace="other-tasks")
    other = replace(
        document,
        release=replace(
            document.release,
            catalog_id=document.workspace_id,
            types=(replace(definition, consensus_policy=policy),),
        ),
        bindings=(replace(document.bindings[0], policy_digest=policy.digest),),
    )
    assert initialize(store, other) == other
    assert (
        store.load(document.workspace_id, document.release.catalog_id, document.digest) == document
    )


@pytest.mark.parametrize("target", ["document", "release", "owner", "genesis"])
def test_external_anchor_and_registry_checks_detect_rewritten_sqlite_rows(rig, target):
    # SQLite deliberately has no migration triggers; simulate a privileged rewrite.
    store, engine = rig
    document = adoption()
    initialize(store, document)
    with engine.begin() as connection:
        if target == "document":
            connection.execute(
                adoptions.update().values(
                    document=document.to_json().replace(
                        '"release_revision":"1"', '"release_revision":"2"'
                    )
                )
            )
        elif target == "release":
            connection.execute(adoptions.update().values(release_digest="f" * 64))
        elif target == "owner":
            connection.execute(owners.update().values(stream_id=document.workspace_id))
        else:
            connection.execute(domains.update().values(genesis_digest="f" * 64))
    with pytest.raises(PolicyError):
        store.load(document.workspace_id, document.release.catalog_id, document.digest)


def test_authenticated_read_only_api_and_real_shared_client_validation(rig):
    store, _ = rig
    document = adoption()
    initialize(store, document)
    token = "synthetic-workspace-token-" + "x" * 40
    app = FastAPI()
    app.include_router(communicationRouter(store.sessions, {document.workspace_id: token}))
    suffix = f"policies/{document.release.catalog_id}/adoptions/{document.digest}"
    url = f"/v1/communication/workspaces/{document.workspace_id}/{suffix}"
    with TestClient(app) as http:
        with patch.object(
            PolicyAdoptionStore, "load", side_effect=AssertionError("must authorize first")
        ):
            assert http.get(url).status_code == 403
            assert (
                http.get(
                    url.replace(document.workspace_id, document.release.catalog_id),
                    headers={"Authorization": "Bearer " + token},
                ).status_code
                == 403
            )
        http.headers["Authorization"] = "Bearer " + token
        assert http.post(url, json=document.to_dict()).status_code == 405
        assert http.get(url + "?latest=true").status_code == 422
        assert http.get(url.replace(document.digest, "f" * 64)).status_code == 404
        assert http.get(url.replace(document.digest, "malformed")).status_code == 422
        response = http.get(url)
        assert response.status_code == 200 and response.json() == document.to_dict()
        client = SynClient("http://127.0.0.1", token)
        # urllib-compatible real response bytes exercise strict JSON parsing too.
        from io import BytesIO

        with patch.object(client._opener, "open", return_value=BytesIO(response.content)) as opened:
            assert (
                client.policy_adoption(
                    document.workspace_id,
                    catalog_id=document.release.catalog_id,
                    expected_adoption_digest=document.digest,
                )
                == document
            )
            request = opened.call_args.args[0]
            assert request.full_url.endswith(suffix)
            assert request.headers["Authorization"] == "Bearer " + token
        changed = replace(document, release=replace(document.release, release_revision="2"))
        with patch.object(client._opener, "open", return_value=BytesIO(changed.to_json().encode())):
            with pytest.raises(IntegrityError):
                client.policy_adoption(
                    document.workspace_id,
                    catalog_id=document.release.catalog_id,
                    expected_adoption_digest=document.digest,
                )


def test_duplicate_server_json_is_rejected_before_pin_validation():
    from io import BytesIO

    document = adoption()
    duplicate = document.to_json().replace(
        '"trust_basis":', '"trust_basis":"duplicate","trust_basis":'
    )
    client = SynClient("http://127.0.0.1", "x" * 40)
    with patch.object(client._opener, "open", return_value=BytesIO(duplicate.encode())):
        with pytest.raises(SynError):
            client.policy_adoption(
                document.workspace_id,
                catalog_id=document.release.catalog_id,
                expected_adoption_digest=document.digest,
            )


def test_real_http_fetch_checks_pins_and_never_initializes_missing_adoption(rig):
    import socket
    import time
    from threading import Thread

    import uvicorn

    store, _engine = rig
    document = adoption()
    initialize(store, document)
    token = "synthetic-test-token-" + "x" * 40
    app = FastAPI()
    app.include_router(communicationRouter(store.sessions, {document.workspace_id: token}))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        client = SynClient(f"http://127.0.0.1:{listener.getsockname()[1]}", token)
        assert (
            client.policy_adoption(
                document.workspace_id,
                catalog_id=document.release.catalog_id,
                expected_adoption_digest=document.digest,
            )
            == document
        )
        with pytest.raises(SynError):
            client.policy_adoption(
                document.workspace_id,
                catalog_id=document.release.catalog_id,
                expected_adoption_digest="f" * 64,
            )
        with pytest.raises(SynError):
            SynClient(client.url, "wrong-" + "x" * 40).policy_adoption(
                document.workspace_id,
                catalog_id=document.release.catalog_id,
                expected_adoption_digest=document.digest,
            )
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
    assert not thread.is_alive()


def test_bootstrap_requires_external_pin_before_database_access_and_does_not_leak(tmp_path, capsys):
    document = adoption()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(document.to_json(), encoding="utf-8")
    with patch("syn.policy_bootstrap.create_engine", side_effect=AssertionError("pins first")):
        assert (
            main(
                [
                    "initialize",
                    "--manifest",
                    str(manifest),
                    "--config",
                    "unused-private-config",
                    "--workspace",
                    document.workspace_id,
                    "--catalog",
                    document.release.catalog_id,
                    "--expected-adoption-digest",
                    "f" * 64,
                ]
            )
            == 1
        )
    assert "unused-private-config" not in capsys.readouterr().out


def test_local_bootstrap_commits_only_to_explicit_test_database(rig, tmp_path, capsys):
    _store, engine = rig
    document = adoption()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(document.to_json(), encoding="utf-8")
    config = tmp_path / "syn.toml"
    config.write_text('[database]\nurl="sqlite://"\n', encoding="utf-8")
    with patch("syn.policy_bootstrap.create_engine", return_value=engine):
        assert (
            main(
                [
                    "initialize",
                    "--manifest",
                    str(manifest),
                    "--config",
                    str(config),
                    "--workspace",
                    document.workspace_id,
                    "--catalog",
                    document.release.catalog_id,
                    "--expected-adoption-digest",
                    document.digest,
                ]
            )
            == 0
        )
    assert document.digest in capsys.readouterr().out
