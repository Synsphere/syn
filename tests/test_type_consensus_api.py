"""First-class policy authoring stays separate from trusted release adoption."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from syn.api import createApp
from syn.controllers.common import databaseSession
from syn.models import Type
from syn.refs import ObjectRef
from syn_client.consensus import CanonicalTypeDefinition, ConsensusPolicy


def policy():
    return {
        "profile": "syn.consensus-policy/1",
        "policy_revision": "1",
        "protocol": "syn.operator-log/1",
        "custody_constraint": "same-verified-issuer",
        "membership_anchor_digest": "a" * 64,
        "participant_reference": "node-public-key-sha256",
        "quorum_rule": "strict-majority",
        "fault_model": "crash-and-partition",
        "proof_profile": "syn.operator-commit/1",
        "conflict_namespace": "tasks",
        "change_rule": "current-policy-and-type-governance",
    }


@pytest.fixture
def authoring():
    record = Type(id=ObjectRef("syn://type/task"), name="Task", consensusPolicy=policy())
    session = Mock(spec=Session)
    session.get.return_value = record
    session.scalars.return_value.all.return_value = []
    app = createApp(Mock(), {})
    app.dependency_overrides[databaseSession] = lambda: session
    return TestClient(app), session, record


def test_old_client_does_not_erase_policy_and_explicit_null_removes_draft(authoring):
    client, session, record = authoring
    result = client.put("/api/type/task", json={"name": "Task"})
    assert result.status_code == 200
    assert result.json()["consensusPolicy"] == policy()
    assert record.consensusPolicy == policy()
    result = client.put("/api/type/task", json={"name": "Task", "consensusPolicy": None})
    assert result.status_code == 200
    assert record.consensusPolicy is None
    assert session.commit.call_count == 2


@pytest.mark.parametrize(
    "change",
    [
        {"protocol": "unknown/1"},
        {"participant_reference": "operator-id"},
        {"fault_model": "trust-everyone"},
        {"future": "field"},
    ],
)
def test_invalid_policy_is_rejected_before_database_access(authoring, change):
    client, session, _ = authoring
    result = client.put(
        "/api/type/task", json={"name": "Task", "consensusPolicy": policy() | change}
    )
    assert result.status_code == 422
    session.get.assert_not_called()
    session.commit.assert_not_called()


def test_canonical_export_is_explicit_draft_and_excludes_private_metadata(authoring):
    client, session, record = authoring
    record.knowledge = {"secret": "must-not-export"}
    result = client.get("/api/type/task/consensus-definition?revision=1")
    assert result.status_code == 200
    body = result.json()
    definition = CanonicalTypeDefinition.from_dict(body["definition"])
    assert body["status"] == "draft"
    assert body["digest"] == definition.digest
    assert definition.consensus_policy == ConsensusPolicy.from_dict(policy())
    assert "must-not-export" not in result.text
    assert "knowledge" not in result.text
    session.commit.assert_not_called()
    assert client.get("/api/type/task/consensus-definition?revision=01").status_code == 422
