from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError

from syn import createEngine, loadConfig


MODEL_TABLES = {
    "type": "types",
    "thing": "things",
    "event": "events",
    "ember": "embers",
    "cog": "cogs",
    "enigma": "enigmas",
    "pondle": "pondles",
    "snug": "snugs",
    "om": "oms",
}


@pytest.fixture(scope="session")
def databaseUrl() -> str:
    url = os.getenv("SYN_TEST_DATABASE_URL")
    if not url:
        configPath = Path(os.getenv("SYN_CONFIG", "config.toml"))
        if not configPath.exists():
            pytest.skip("Set SYN_TEST_DATABASE_URL or create config.toml for database tests")
        url = loadConfig(configPath).database.url

    if "change-me" in url:
        pytest.skip("Replace the placeholder password in config.toml for database tests")
    return url


@pytest.fixture(scope="session")
def databaseEngine(databaseUrl: str) -> Iterator[Engine]:
    engine = createEngine(databaseUrl)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            installed = connection.scalar(text("SELECT to_regclass('\"objectRegistry\"')"))
            if installed is None:
                pytest.fail(
                    "PostgreSQL is reachable but the Syn schema is not installed; "
                    "run database/objectReferenceSchema.sql first"
                )
    except SQLAlchemyError as error:
        pytest.skip(f"PostgreSQL connection unavailable: {error}")

    yield engine
    engine.dispose()


@pytest.fixture
def db(databaseEngine: Engine) -> Iterator[Connection]:
    with databaseEngine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()
