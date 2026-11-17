"""Durable fixed-delay schedules for the existing ProcessScheduler.

No execution loop lives here: due entries become ordinary persisted Syn Jobs.
Missed intervals coalesce. A schedule retains at most one unfinished Job.
"""
from __future__ import annotations
from datetime import UTC, datetime, timedelta
import math
from uuid import uuid4
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, DateTime, Float,
                        ForeignKey, Index, JSON, MetaData, String, Table, Uuid, select)

metadata = MetaData()
jobs = Table("jobs", metadata, Column("id", Uuid, primary_key=True))
schedules = Table("jobSchedules", metadata,
    Column("scheduleKey", String(128), primary_key=True),
    Column("schedulerKey", String(128), nullable=False),
    Column("handler", String(128), nullable=False),
    Column("arguments", JSON, nullable=False),
    Column("intervalSeconds", Float, nullable=False),
    Column("nextRunAt", DateTime(timezone=True), nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("currentJobId", Uuid, ForeignKey("jobs.id")),
    Column("failures", BigInteger, nullable=False),
    CheckConstraint('"intervalSeconds" >= 1 AND "intervalSeconds" <= 86400', name="jobScheduleIntervalCheck"),
    CheckConstraint('failures >= 0', name="jobScheduleFailuresCheck"),
    Index("jobSchedulesDueIdx", "schedulerKey", "enabled", "nextRunAt"),
)
dispatch = Table("jobDispatch", metadata,
    Column("jobId", Uuid, ForeignKey("jobs.id"), primary_key=True),
    Column("schedulerKey", String(128), nullable=False, index=True),
    Column("scheduleKey", String(128), ForeignKey("jobSchedules.scheduleKey")),
)
RECURRENCE_TABLES = (schedules, dispatch)


def utc(value):
    # DB contract is UTC. SQLite unit fixtures discard tzinfo; external samples
    # must instead use positions.types.timestamp, which rejects naive values.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def ensure_schedule(session, key, schedulerKey, handler, arguments, intervalSeconds, *, now=None):
    from .positions.schema import insert_ignore
    if any(not isinstance(x, str) or not 1 <= len(x) <= 128 for x in (key, schedulerKey, handler)):
        raise ValueError("Invalid schedule identifier")
    if type(intervalSeconds) not in (int, float) or not math.isfinite(intervalSeconds) or not 1 <= intervalSeconds <= 86400:
        raise ValueError("Schedule interval must be finite and in [1,86400]")
    now = now or datetime.now(UTC)
    insert_ignore(session, schedules, {"scheduleKey": key, "schedulerKey": schedulerKey, "handler": handler,
        "arguments": arguments, "intervalSeconds": intervalSeconds, "nextRunAt": now,
        "enabled": True, "currentJobId": None, "failures": 0}, ["scheduleKey"])
    old = session.execute(select(schedules).where(schedules.c.scheduleKey == key).with_for_update()).mappings().one()
    if old["schedulerKey"] != schedulerKey or old["handler"] != handler:
        raise ValueError("A schedule key is already owned by a different scheduler or handler")
    session.execute(schedules.update().where(schedules.c.scheduleKey == key).values(
        arguments=arguments, intervalSeconds=intervalSeconds, enabled=True))


def bind_job(session, jobId, schedulerKey, scheduleKey=None):
    session.execute(dispatch.insert().values(jobId=jobId, schedulerKey=schedulerKey, scheduleKey=scheduleKey))


def materialize_due(session, schedulerKey, *, now=None, limit=32):
    """Atomically materialize due normal Jobs. Caller holds scheduler ownership."""
    from .models import Job, JobStatus
    now = now or datetime.now(UTC)
    rows = session.execute(select(schedules).where(schedules.c.schedulerKey == schedulerKey)
        .order_by(schedules.c.nextRunAt).with_for_update(skip_locked=True)).mappings().all()
    added = []
    for row in rows:
        nextRun, failures, current = utc(row["nextRunAt"]), row["failures"], row["currentJobId"]
        if current:
            job = session.get(Job, current)
            if job is None:
                raise RuntimeError("Schedule references an absent persisted Job")
            if job.status in (JobStatus.queued, JobStatus.running):
                continue
            failures = min(failures+1, 16) if job.status == JobStatus.failed else 0
            delay = row["intervalSeconds"] * 2**min(max(failures-1, 0), 10)
            delay = min(delay, max(3600, row["intervalSeconds"]))
            nextRun = utc(job.finishedAt or now)+timedelta(seconds=delay)
            session.execute(schedules.update().where(schedules.c.scheduleKey == row["scheduleKey"])
                .values(currentJobId=None, failures=failures, nextRunAt=nextRun))
        if not row["enabled"] or nextRun > now or len(added) >= limit:
            continue
        job = Job(id=uuid4(), handler=row["handler"], arguments=row["arguments"],
                  status=JobStatus.queued, createdAt=now)
        session.add(job)
        session.flush()
        bind_job(session, job.id, schedulerKey, row["scheduleKey"])
        session.execute(schedules.update().where(schedules.c.scheduleKey == row["scheduleKey"])
                        .values(currentJobId=job.id))
        added.append(job.id)
    return added
