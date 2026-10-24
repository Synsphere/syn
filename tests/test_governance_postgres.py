"""Actual PostgreSQL serialization, rollback and immutable signed history."""

import importlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from syn_client.consensus import PolicyError, canonical_bytes, load_document

from syn.governance import GovernedPolicyStore
from syn.policy_adoption import PolicyConflict, signedRecords
from test_governance import CATALOG, NOW, admit, genesis, successor
from test_policy_adoption_postgres import database as _owned_database
from test_dag_finality import valid_pair

pytestmark = pytest.mark.database
TABLES = ("syn_governed_catalogs", "syn_governed_records", "syn_governed_namespace_claims")


@pytest.fixture(scope="module")
def governance_database():
    yield from _owned_database.__wrapped__()


def test_federated_replay_is_verified_before_postgres_append(governance_database):
    legacy, engine = governance_database
    store = GovernedPolicyStore(legacy.sessions)
    root, one = valid_pair()
    approvals = load_document(one.approvals_json)
    approvals["type_governance"] = []
    bad = replace(one, approvals_json=canonical_bytes(approvals).decode())
    admit(store, root, root)
    with pytest.raises(PolicyError):
        admit(store, bad, root)
    with engine.connect() as c:
        assert (
            len(
                c.execute(
                    select(signedRecords).where(
                        signedRecords.c.workspace_id == root.state.workspace_id
                    )
                ).all()
            )
            == 1
        )
    admit(store, one, root)
    assert store.chain(root.state.workspace_id, CATALOG, root.digest, one.digest) == (root, one)


def test_concurrent_signed_initialization_and_successors_have_one_head(governance_database):
    _legacy, engine = governance_database
    store = GovernedPolicyStore(_legacy.sessions)
    root = genesis(str(uuid4()))
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: admit(store, root, root), range(4))) == [root] * 4
    candidates = (successor(root, name="One"), successor(root, name="Two"))
    barrier = Barrier(2)

    def attempt(record):
        barrier.wait(timeout=10)
        try:
            return admit(store, record, root)
        except PolicyConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, candidates))
    assert sum(r is not None for r in results) == 1
    winner = next(r for r in results if r is not None)
    assert store.chain(root.state.workspace_id, CATALOG, root.digest, winner.digest) == (
        root,
        winner,
    )
    assert admit(GovernedPolicyStore(_legacy.sessions), winner, root, now=NOW + 10000) == winner
    with engine.connect() as c:
        assert (
            len(
                c.execute(
                    select(signedRecords).where(
                        signedRecords.c.workspace_id == root.state.workspace_id
                    )
                ).all()
            )
            == 2
        )


def test_rejected_approvals_do_not_advance_the_head(governance_database):
    legacy, _engine = governance_database
    store = GovernedPolicyStore(legacy.sessions)
    root = genesis(str(uuid4()))
    admit(store, root, root)
    record = successor(root)
    approvals = load_document(record.approvals_json)
    approvals["existing_policies"] = {}
    invalid = replace(record, approvals_json=canonical_bytes(approvals).decode())
    with pytest.raises(PolicyError):
        admit(store, invalid, root)
    assert store.chain(root.state.workspace_id, CATALOG, root.digest, root.digest) == (root,)
    admit(store, record, root)


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_database_refuses_signed_history_rewrites(governance_database, table, operation):
    legacy, engine = governance_database
    store = GovernedPolicyStore(legacy.sessions)
    root = genesis(str(uuid4()))
    admit(store, root, root)
    sql = {
        "UPDATE": f"UPDATE {table} SET catalog_id = catalog_id",
        "DELETE": f"DELETE FROM {table}",
        "TRUNCATE": f"TRUNCATE {table} CASCADE",
    }[operation]
    with pytest.raises(DBAPIError, match="immutable"):
        with engine.begin() as c:
            c.execute(text(sql))
    assert store.chain(root.state.workspace_id, CATALOG, root.digest, root.digest) == (root,)


def test_signed_migration_is_reversible_in_owned_schema(governance_database):
    _legacy, engine = governance_database
    migration = importlib.import_module("syn.alembic.versions.20261024_0015_signed_governance")
    with engine.begin() as c:
        with Operations.context(MigrationContext.configure(c)):
            migration.downgrade()
            migration.upgrade()
        assert c.scalar(text("SELECT count(*) FROM syn_governed_records")) == 0
