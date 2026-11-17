"""Real migrations, domains, deferred constraints and concurrent node ownership.

Opt in with SYN_POSITION_TEST_DATABASE_URL pointing at a disposable PostgreSQL
DATABASE. Tests create/drop only their own random schema, but require DDL rights.
All Syn dependencies and the full repository must be installed. These tests are
not substituted with SQLite when PostgreSQL is unavailable.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

URL = os.environ.get("SYN_POSITION_TEST_DATABASE_URL")
pytestmark = [pytest.mark.database, pytest.mark.skipif(not URL, reason="Real PostgreSQL opt-in URL is not configured")]


@pytest.fixture(scope="module")
def postgres():
    from syn.database import createEngine
    from syn.positions.store import PositionStore
    name = "syn_position_test_" + uuid4().hex
    admin = create_engine(URL)
    engine = None
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{name}"'))
        # Do not inherit public's alembic_version or tables during the upgrade.
        scoped = make_url(URL).update_query_dict({"options": f"-csearch_path={name}"})
        engine = createEngine(scoped.render_as_string(hide_password=False), migrate=True)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        yield PositionStore(sessions), sessions
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        admin.dispose()


def setup_node(store):
    from syn.positions.types import geographic
    now = datetime.now(UTC)
    node = "syn://thing/pg-test-" + uuid4().hex
    store.bootstrap(node, geographic(51, 0, None, 100000, now, "timezone"))
    return node, now


def test_full_migration_and_required_inheritance(postgres):
    store, sessions = postgres
    node, _ = setup_node(store)
    assert store.verify(node)["objects"] == 1
    with sessions() as session:
        parents = session.execute(text('SELECT "parentRef"::text FROM "typeParents" WHERE "childRef"=\'syn://type/gps-reading\'')).scalars().all()
        assert set(parents) == {"syn://type/geographic-geodetic", "syn://type/event"}
        assert session.execute(text('SELECT version_num FROM "alembicVersion"')).scalar_one() == "20261117_0017"


def test_database_blocks_history_rewrite_and_unledgered_pointer(postgres):
    from syn.positions import schema
    store, sessions = postgres
    node, _ = setup_node(store)
    reference = store.current(node)[0]["emberRef"]
    with pytest.raises(DBAPIError):
        with sessions.begin() as session:
            session.execute(schema.primitive["embers"].update().where(schema.primitive["embers"].c.id == reference).values(knowledge={}))
    with pytest.raises(DBAPIError):
        with sessions.begin() as session:
            session.execute(schema.positions.update().where(schema.positions.c.nodeRef == node).values(revision=999))
    assert store.verify(node)["objects"] == 1


def test_concurrent_duplicate_readings_have_one_event_and_one_replacement(postgres):
    from syn.positions.types import geographic, Reading
    store, _ = postgres
    node, now = setup_node(store)
    reading = Reading("synthetic-gps", now, now, geographic(51.5, -.1, None, 4, now, "synthetic-gps"), "2d")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.observe(node, reading), range(4)))
    assert sorted(r["decision"] for r in results) == ["duplicate", "duplicate", "duplicate", "replace"]
    assert store.verify(node)["objects"] == 3


def test_postgres_scheduler_ownership_is_exclusive(postgres):
    from syn.scheduler import ProcessScheduler
    _, sessions = postgres
    key = "pg-test-" + uuid4().hex
    first = ProcessScheduler(sessions, {}, schedulerKey=key)
    second = ProcessScheduler(sessions, {}, schedulerKey=key)
    try:
        first._acquire()
        with pytest.raises(RuntimeError, match="owns"):
            second._acquire()
        first._release()
        second._acquire()
    finally:
        first._release()
        second._release()
