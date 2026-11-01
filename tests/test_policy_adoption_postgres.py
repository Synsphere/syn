"""Real PostgreSQL acceptance, confined to one uniquely owned disposable schema."""

import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from syn.migrations import upgradeDatabase
from syn.policy_adoption import PolicyAdoptionStore, PolicyConflict, adoptions, domains, owners
from test_policy_adoption import adoption, initialize

pytestmark = pytest.mark.database
TABLES = (
    "syn_policy_workspaces",
    "syn_policy_adoptions",
    "syn_policy_domains",
    "syn_policy_conflict_owners",
)


@pytest.fixture(scope="module")
def database():
    url = os.environ.get("SYN_COMMUNICATION_TEST_URL")
    if not url:
        pytest.skip("Set the explicit disposable-schema PostgreSQL acceptance URL")
    schema = "syn_policy_genesis_test_" + uuid4().hex
    admin = create_engine(url)
    engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema + ",public"})
    try:
        with admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))
            connection.execute(
                text(
                    'CREATE TABLE "' + schema + '"."alembicVersion" '
                    "(version_num varchar(32) NOT NULL PRIMARY KEY)"
                )
            )
        upgradeDatabase(engine)
        yield PolicyAdoptionStore(sessionmaker(engine)), engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA IF EXISTS "' + schema + '" CASCADE'))
        admin.dispose()


def test_concurrent_exact_retries_remain_one_durable_adoption(database):
    store, engine = database
    document = replace(adoption(), workspace_id=str(uuid4()))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: initialize(store, document), range(16)))
    assert results == [document] * 16
    with engine.connect() as connection:
        assert (
            len(
                connection.execute(
                    select(adoptions).where(adoptions.c.workspace_id == document.workspace_id)
                ).all()
            )
            == 1
        )
    reopened = PolicyAdoptionStore(sessionmaker(engine))
    assert (
        reopened.load(document.workspace_id, document.release.catalog_id, document.digest)
        == document
    )


@pytest.mark.parametrize("fork", ["catalog", "domain", "namespace"])
def test_concurrent_forks_cannot_self_ratify_or_claim_one_namespace(database, fork):
    store, engine = database
    first = replace(adoption(), workspace_id=str(uuid4()))
    if fork == "catalog":
        second = replace(first, release=replace(first.release, release_revision="2"))
    else:
        second = replace(first, release=replace(first.release, catalog_id=str(uuid4())))
        if fork == "domain":
            second = replace(
                second, bindings=(replace(second.bindings[0], genesis_digest="f" * 64),)
            )
    barrier = Barrier(2)

    def attempt(document):
        barrier.wait(timeout=10)
        try:
            return initialize(store, document)
        except PolicyConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (first, second)))
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result is not None)
    assert store.load(winner.workspace_id, winner.release.catalog_id, winner.digest) == winner
    with engine.connect() as connection:
        for table in (adoptions, domains, owners):
            assert (
                len(
                    connection.execute(
                        select(table).where(table.c.workspace_id == first.workspace_id)
                    ).all()
                )
                == 1
            )


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_postgres_refuses_update_delete_and_truncate(database, table, operation):
    store, engine = database
    document = replace(adoption(), workspace_id=str(uuid4()))
    initialize(store, document)
    statement = {
        "UPDATE": f"UPDATE {table} SET workspace_id = workspace_id",
        "DELETE": f"DELETE FROM {table}",
        "TRUNCATE": f"TRUNCATE {table} CASCADE",
    }[operation]
    with pytest.raises(DBAPIError, match="immutable"):
        with engine.begin() as connection:
            connection.execute(text(statement))
    assert (
        store.load(document.workspace_id, document.release.catalog_id, document.digest) == document
    )


def test_migration_can_downgrade_and_reupgrade_only_owned_schema(database):
    _store, engine = database
    migration = importlib.import_module("syn.alembic.versions.20261023_0014_policy_genesis")
    successor = importlib.import_module("syn.alembic.versions.20261024_0015_signed_governance")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            successor.downgrade()
            migration.downgrade()
            migration.upgrade()
            successor.upgrade()
        assert connection.scalar(text("SELECT count(*) FROM syn_policy_adoptions")) == 0
