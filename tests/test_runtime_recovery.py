"""Startup schedule recovery with real persistence and API supervision.

SQLite supplies the default Jobs projection. An explicit
SYN_RUNTIME_RECOVERY_TEST_DATABASE_URL runs the same assertions against
PostgreSQL in a fresh, isolated schema. Windows authority access is not mocked
into a claim of native Windows execution.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
import os
from threading import Event
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from syn.models import Job, JobStatus
from syn.recurrence import (
    RECURRENCE_TABLES, bind_job, ensure_schedule, materialize_due, metadata,
    schedules, utc,
)
from syn.scheduler import ProcessScheduler
from syn.witnessing.runtime import check_local_jobs


@pytest.fixture
def persisted(tmp_path):
    url = os.environ.get("SYN_RUNTIME_RECOVERY_TEST_DATABASE_URL")
    admin, engine, schema = None, None, None
    try:
        if url:
            admin = create_engine(url)
            if admin.dialect.name != "postgresql":
                raise ValueError("The explicit recovery test database must be PostgreSQL")
            candidate = "syn_runtime_recovery_" + uuid4().hex
            with admin.begin() as connection:
                connection.exec_driver_sql(f'CREATE SCHEMA "{candidate}"')
            schema = candidate
            engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema})
            with engine.begin() as connection:
                Job.__table__.c.status.type.create(connection, checkfirst=False)
                Job.__table__.create(connection)
        else:
            engine = create_engine("sqlite:///" + str(tmp_path / "jobs.db"))
            # The actual Job ORM is used; only PostgreSQL-specific DDL is replaced.
            with engine.begin() as connection:
                connection.execute(text("""CREATE TABLE jobs (
                    id CHAR(32) PRIMARY KEY, handler TEXT NOT NULL,
                    arguments JSON NOT NULL, status VARCHAR(32) NOT NULL,
                    result JSON, error TEXT, "createdAt" DATETIME NOT NULL,
                    "startedAt" DATETIME, "finishedAt" DATETIME
                )"""))
        metadata.create_all(engine, tables=RECURRENCE_TABLES)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        yield sessions
    finally:
        if engine is not None:
            engine.dispose()
        if admin is not None:
            try:
                if schema is not None:
                    # This name was generated above, never taken from config/input.
                    with admin.begin() as connection:
                        connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            finally:
                admin.dispose()


def seed(sessions, key="operator-dag-owner", scope="node-a", status=JobStatus.failed,
         handler="operator-dag-sync"):
    current = uuid4()
    now = datetime.now(UTC) - timedelta(minutes=5)
    with sessions.begin() as session:
        ensure_schedule(session, key, scope, handler, {"schedulerKey": scope}, 120, now=now)
        session.add(Job(id=current, handler=handler, arguments={"schedulerKey": scope},
            status=status, error="retained failure evidence" if status == JobStatus.failed else None,
            createdAt=now, startedAt=now if status != JobStatus.queued else None,
            finishedAt=now if status == JobStatus.failed else None))
        session.flush()
        bind_job(session, current, scope, key)
        session.execute(schedules.update().where(schedules.c.scheduleKey == key).values(
            currentJobId=current, failures=2))
    return current


@pytest.mark.parametrize("prior_status", [JobStatus.failed, JobStatus.running, JobStatus.queued])
def test_verified_startup_recovers_only_its_schedule_and_preserves_audit(persisted, prior_status):
    old = seed(persisted, status=prior_status)
    unrelated = seed(persisted, key="other-duty", handler="another-handler")
    foreign = seed(persisted, key="operator-dag-other", scope="node-b")
    calls = []
    result = {"operator": "owner", "nodes": 4, "signed": True}

    def sync(**arguments):
        calls.append(arguments)
        return result

    manager = ProcessScheduler(persisted, {"operator-dag-sync": sync}, schedulerKey="node-a")

    async def exercise():
        await manager.start(prepare=lambda current: current.reconcile_startup_schedule(
            "operator-dag-owner", "operator-dag-sync", {"schedulerKey": "node-a"}, 120))
        try:
            check_local_jobs(persisted, "node-a")
            with pytest.raises(RuntimeError, match="lost readiness"):
                check_local_jobs(persisted, "node-b")
            with persisted() as session:
                recovered = session.execute(select(schedules).where(
                    schedules.c.scheduleKey == "operator-dag-owner")).mappings().one()
                completed = session.scalars(select(Job).where(
                    Job.handler == "operator-dag-sync", Job.status == JobStatus.succeeded)).one()
                assert recovered["failures"] == 0
                assert utc(recovered["nextRunAt"]) == utc(completed.finishedAt) + timedelta(seconds=120)
                assert completed.result == result
                assert utc(completed.startedAt) <= utc(completed.finishedAt)
                previous = session.get(Job, old)
                if prior_status == JobStatus.failed:
                    assert previous.status == JobStatus.failed
                    assert previous.error == "retained failure evidence"
                else:
                    assert previous.status == JobStatus.canceled
                    assert str(completed.id) in previous.error
                assert session.get(Job, unrelated).status == JobStatus.failed
                assert session.get(Job, foreign).status == JobStatus.failed
                untouched = session.execute(select(schedules).where(
                    schedules.c.scheduleKey.in_(["other-duty", "operator-dag-other"]))).mappings().all()
                assert all(row["failures"] > 0 for row in untouched)
            # The first pump/recovery cycle cannot turn the old running/queued
            # job back into duplicate work or increment the old failure again.
            await asyncio.sleep(.3)
            assert calls == [{"schedulerKey": "node-a"}]
            check_local_jobs(persisted, "node-a")
        finally:
            await manager.stop()

    asyncio.run(exercise())


def test_failed_startup_cannot_clear_a_previous_failure(persisted):
    old = seed(persisted)

    def unavailable(**_):
        raise RuntimeError("Host custody unavailable")

    manager = ProcessScheduler(persisted, {"operator-dag-sync": unavailable}, schedulerKey="node-a")

    async def exercise():
        with pytest.raises(RuntimeError, match="Host custody unavailable"):
            await manager.start(prepare=lambda current: current.reconcile_startup_schedule(
                "operator-dag-owner", "operator-dag-sync", {"schedulerKey": "node-a"}, 120))
        assert not manager.running and manager._lease is None and not manager._preparing

    asyncio.run(exercise())
    with persisted() as session:
        row = session.execute(select(schedules)).mappings().one()
        assert row["failures"] == 2 and row["currentJobId"] == old
        jobs = session.scalars(select(Job)).all()
        assert len(jobs) == 1 and jobs[0].status == JobStatus.failed
        assert jobs[0].error == "retained failure evidence"
    with pytest.raises(RuntimeError, match="lost readiness"):
        check_local_jobs(persisted, "node-a")


def test_reconciliation_requires_preparation_and_cannot_take_another_scope(persisted):
    seed(persisted, scope="node-b")
    calls = []
    manager = ProcessScheduler(persisted,
        {"operator-dag-sync": lambda **kwargs: calls.append(kwargs)}, schedulerKey="node-a")
    with pytest.raises(RuntimeError, match="preparation ownership"):
        manager.reconcile_startup_schedule("operator-dag-owner", "operator-dag-sync", {}, 120)

    async def exercise():
        with pytest.raises(ValueError, match="different scheduler"):
            await manager.start(prepare=lambda current: current.reconcile_startup_schedule(
                "operator-dag-owner", "operator-dag-sync", {"schedulerKey": "node-a"}, 120))
        assert manager._lease is None

    asyncio.run(exercise())
    assert calls == []


def test_a_new_failure_after_recovery_still_fences(persisted):
    seed(persisted)
    manager = ProcessScheduler(persisted, {"operator-dag-sync": lambda **_: {"signed": True}},
                               schedulerKey="node-a")

    async def exercise():
        await manager.start(prepare=lambda current: current.reconcile_startup_schedule(
            "operator-dag-owner", "operator-dag-sync", {"schedulerKey": "node-a"}, 120))
        await manager.stop()

    asyncio.run(exercise())
    check_local_jobs(persisted, "node-a")
    with persisted.begin() as session:
        row = session.execute(select(schedules)).mappings().one()
        due = utc(row["nextRunAt"]) + timedelta(seconds=1)
        new_job = materialize_due(session, "node-a", now=due)[0]
        job = session.get(Job, new_job)
        job.status = JobStatus.failed
        job.finishedAt = due
        job.error = "new authority failure"
    with pytest.raises(RuntimeError, match="lost readiness"):
        check_local_jobs(persisted, "node-a")


@pytest.mark.parametrize("barrier", ["handler", "commit"])
def test_cancelled_preparation_joins_worker_before_releasing_ownership(persisted, barrier):
    old = seed(persisted)
    entered, release, finished = Event(), Event(), Event()

    def wait_for_release():
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Test startup barrier was not released")

    def sync(**_):
        if barrier == "handler":
            wait_for_release()
        return {"signed": True}

    def before_commit(session):
        # Stop the final transaction after it contains the succeeded audit Job,
        # exactly where a pre-transaction ownership check cannot protect it.
        if barrier == "commit" and not entered.is_set():
            completed = session.scalar(select(Job.id).where(Job.status == JobStatus.succeeded))
            if completed is not None:
                wait_for_release()

    event.listen(persisted.class_, "before_commit", before_commit)
    manager = ProcessScheduler(persisted, {"operator-dag-sync": sync}, schedulerKey="node-a")
    contender = ProcessScheduler(persisted, {}, schedulerKey="node-a")

    def prepare(current):
        try:
            current.reconcile_startup_schedule(
                "operator-dag-owner", "operator-dag-sync", {"schedulerKey": "node-a"}, 120)
        finally:
            finished.set()

    async def exercise():
        task = asyncio.create_task(manager.start(prepare=prepare))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            lease = manager._lease
            assert lease is not None and not lease.closed
            task.cancel()
            await asyncio.sleep(.02)
            # Repeat cancellation as asyncio.run shutdown does: directly cancel
            # every pending Task, including any shielded child worker Task.
            for pending in asyncio.all_tasks() - {asyncio.current_task()}:
                pending.cancel()
            await asyncio.sleep(.02)
            assert not task.done() and not finished.is_set()
            assert manager._preparing and manager._lease is lease and not lease.closed
            if lease.dialect.name == "postgresql":
                with pytest.raises(RuntimeError, match="Another ProcessScheduler owns"):
                    await contender.start()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert finished.is_set() and task.cancelled()
            assert not manager._preparing and manager._lease is None
            assert not manager.running and manager._pumpTask is None
            with persisted() as session:
                assert session.get(Job, old).status == JobStatus.failed
                assert session.scalars(select(Job).where(Job.status == JobStatus.succeeded)).one().result == {"signed": True}
            if lease.dialect.name == "postgresql":
                # The next owner can acquire only after the first worker's
                # transaction has completed and cancellation released the lease.
                await contender.start()
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert await asyncio.to_thread(finished.wait, 3)
            await manager.stop()
            await contender.stop()

    try:
        asyncio.run(exercise())
    finally:
        event.remove(persisted.class_, "before_commit", before_commit)


def test_api_readiness_log_names_component_and_types_without_exception_payload(caplog):
    from syn.api import createApp
    from syn.config import P2PConfig

    app = createApp(sessionmaker(), {}, P2PConfig(enabled=False))
    secret_message = "postgresql://private-user:private-password@database/private-db"
    with caplog.at_level(logging.ERROR, logger="uvicorn.error"), TestClient(app):
        app.state.cardano.fault = TypeError(secret_message)
        deadline = time.monotonic() + 3
        while app.state.awarenessFault is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert app.state.awarenessFault is not None
    messages = [record.getMessage() for record in caplog.records
                if "Syn node lost readiness" in record.getMessage()]
    assert len(messages) == 1
    assert "component=cardano" in messages[0]
    assert "RuntimeError <- TypeError" in messages[0]
    assert secret_message not in messages[0] and "private-password" not in caplog.text


def test_readiness_type_chain_is_bounded_and_handles_cycles():
    from syn.api import _exception_type_chain

    first, second = RuntimeError("private request"), TypeError("private payload")
    first.__cause__, second.__cause__ = second, first
    assert _exception_type_chain(first) == "RuntimeError <- TypeError"
