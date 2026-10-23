"""Run the new migration and ORM against a uniquely owned disposable schema."""

import importlib
import os
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from syn.migrations import upgradeDatabase
from syn.models import Type
from syn.refs import ObjectRef

pytestmark = pytest.mark.database


def test_policy_column_roundtrip_object_constraint_and_reversible_migration():
    url = os.environ.get("SYN_COMMUNICATION_TEST_URL")
    if not url:
        pytest.skip("Set the explicit disposable-schema PostgreSQL acceptance URL")
    schema = "syn_type_policy_test_" + uuid4().hex
    admin = create_engine(url)
    engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema})
    try:
        with admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))
        upgradeDatabase(engine)
        with Session(engine) as session:
            record = Type(id=ObjectRef("syn://type/consensus-fixture"), name="Fixture")
            session.add(record)
            session.commit()
            assert record.consensusPolicy is None
            record.consensusPolicy = {"local": "draft"}
            session.commit()
            session.expire_all()
            assert session.get(Type, record.id).consensusPolicy == {"local": "draft"}
        with engine.begin() as connection:
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(text("UPDATE types SET \"consensusPolicy\" = '[]'::jsonb"))
        migration = importlib.import_module(
            "syn.alembic.versions.20261023_0013_type_consensus_policy"
        )
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                migration.upgrade()
            assert (
                connection.scalar(
                    text(
                        "SELECT \"consensusPolicy\" FROM types WHERE id='syn://type/consensus-fixture'"
                    )
                )
                is None
            )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA IF EXISTS "' + schema + '" CASCADE'))
        admin.dispose()
