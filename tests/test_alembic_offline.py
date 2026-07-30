from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from alembic import command

from syn.migrations import alembicConfig


def test_offline_baseline_preserves_postgresql_percent_characters() -> None:
    config = alembicConfig()
    output = StringIO()
    config.output_buffer = output
    with patch("syn.config.loadConfig") as load:
        load.return_value.database.url = "postgresql+psycopg://localhost/syn"
        command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "'%[0-9A-Fa-f]{2}'" in sql
    assert "'%%[0-9A-Fa-f]{2}'" not in sql
    assert "cannot delete referenced local ObjectRef %" in sql
    assert "cannot delete referenced local ObjectRef %%" not in sql


def test_offline_head_contains_polyhierarchical_type_graph_and_seed() -> None:
    config = alembicConfig()
    output = StringIO()
    config.output_buffer = output
    with patch("syn.config.loadConfig") as load:
        load.return_value.database.url = "postgresql+psycopg://localhost/syn"
        command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert 'CREATE TABLE "typeParents"' in sql
    assert 'CREATE VIEW "typeAncestry"' in sql
    assert 'CREATE VIEW "objectTypesInherited"' in sql
    assert 'CREATE FUNCTION "objectHasType"' in sql
    assert "syn://type/question" in sql
    assert (
        "A proposition which can be answered, either yes or no, or with any other "
        "kind of response."
    ) in sql


def test_offline_head_contains_feed_ontology_seed() -> None:
    config = alembicConfig()
    output = StringIO()
    config.output_buffer = output
    with patch("syn.config.loadConfig") as load:
        load.return_value.database.url = "postgresql+psycopg://localhost/syn"
        command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "20260730_0003" in sql
    assert "syn://type/feed" in sql
    assert "syn://type/rss-feed" in sql
    assert "syn://type/oai-pmh-feed" in sql
    assert "syn://type/crypto-price-feed" in sql


def test_offline_head_contains_jobs_table() -> None:
    config = alembicConfig()
    output = StringIO()
    config.output_buffer = output
    with patch("syn.config.loadConfig") as load:
        load.return_value.database.url = "postgresql+psycopg://localhost/syn"
        command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert 'CREATE TYPE "jobStatus"' in sql
    assert 'CREATE TABLE "jobs"' in sql
    assert 'CREATE INDEX "jobsStatusCreatedIdx"' in sql
