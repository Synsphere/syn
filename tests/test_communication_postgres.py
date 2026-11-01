"""Opt-in PostgreSQL acceptance in a new, uniquely named disposable schema only."""

from __future__ import annotations

import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from syn_client.protocol import GENESIS_HASH, verify_receipt

from syn.communication import MessageStore

pytestmark = pytest.mark.database


@pytest.fixture
def ledger():
    url = os.environ.get("SYN_COMMUNICATION_TEST_URL")
    if not url:
        pytest.skip(
            "Set SYN_COMMUNICATION_TEST_URL for disposable-schema PostgreSQL acceptance"
        )
    schema = "syn_communication_test_" + uuid4().hex
    admin = create_engine(url)
    engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema})
    try:
        with admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))
        migration = importlib.import_module(
            "syn.alembic.versions.20261023_0012_communication_ledger"
        )
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
        yield MessageStore(sessionmaker(engine)), engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA IF EXISTS "' + schema + '" CASCADE'))
        admin.dispose()


def test_concurrent_writers_have_one_commit_order_and_retry_identity(ledger):
    store, _engine = ledger
    workspace = str(uuid4())
    bodies = [
        {
            "message_id": str(uuid4()),
            "channel": "acceptance",
            "sender": "fixture",
            "payload": {"index": index},
        }
        for index in range(40)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda body: store.append(workspace, body), bodies))
    assert sorted(item["sequence"] for item in receipts) == list(range(1, 41))
    with ThreadPoolExecutor(max_workers=8) as pool:
        retries = list(
            pool.map(lambda _index: store.append(workspace, bodies[0]), range(16))
        )
    assert all(receipt == retries[0] for receipt in retries)
    previous = GENESIS_HASH
    for receipt in store.page(workspace, 0, 100)["items"]:
        verify_receipt(receipt, workspace)
        assert receipt["previous_hash"] == previous
        previous = receipt["hash"]
    assert store.head(workspace) == {
        "workspace_id": workspace,
        "sequence": 40,
        "hash": previous,
    }


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE syn_communication_messages SET sender='changed'",
        "DELETE FROM syn_communication_messages",
        "TRUNCATE syn_communication_messages",
    ],
)
def test_database_rejects_rewriting_or_erasing_receipts(ledger, statement):
    store, engine = ledger
    workspace = str(uuid4())
    store.append(
        workspace,
        {
            "message_id": str(uuid4()),
            "channel": "acceptance",
            "sender": "fixture",
            "payload": {},
        },
    )
    with pytest.raises(DBAPIError, match="append-only"):
        with engine.begin() as connection:
            connection.execute(text(statement))
    assert len(store.page(workspace, 0, 100)["items"]) == 1
