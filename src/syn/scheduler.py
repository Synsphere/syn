from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from scheduler.Scheduler import Scheduler
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Job, JobStatus
from .recurrence import bind_job, dispatch, ensure_schedule, materialize_due, schedules

JobHandler = Callable[..., Any]
log = logging.getLogger(__name__)


class SchedulerNotRunningError(RuntimeError):
    pass


class UnknownHandlerError(ValueError):
    pass


class ProcessScheduler:
    """Persistent one-shot AND recurring Jobs, executed by AsyncProcessScheduler.

    One owner per schedulerKey is fenced by a PostgreSQL session advisory lock.
    Other node scopes can use the same DB without recovering each other's Jobs.
    The default scope retains ownership of pre-migration unbound one-shot Jobs.
    """

    def __init__(self, sessions: sessionmaker[Session], handlers: Mapping[str, JobHandler],
                 *, schedulerKey: str = "default", maxConcurrentJobs: int = 4) -> None:
        if not schedulerKey or len(schedulerKey) > 128 or type(maxConcurrentJobs) is not int or not 1 <= maxConcurrentJobs <= 128:
            raise ValueError("Invalid scheduler scope or concurrency")
        self.sessions = sessions
        self.handlers = dict(handlers)
        self.schedulerKey = schedulerKey
        self.maxConcurrentJobs = maxConcurrentJobs
        self.running = False
        self.fault: BaseException | None = None
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._schedulers: dict[UUID, Scheduler] = {}
        self._lock = asyncio.Lock()
        self._pumpTask = None
        self._lease = None

    def _owned(self):
        criterion = dispatch.c.schedulerKey == self.schedulerKey
        if self.schedulerKey == "default":
            criterion = or_(criterion, dispatch.c.jobId.is_(None))
        return select(Job).outerjoin(dispatch, dispatch.c.jobId == Job.id).where(criterion)

    def _acquire(self):
        with self.sessions() as session:
            engine = session.get_bind()
        self._lease = engine.connect()
        if self._lease.dialect.name != "postgresql":
            # SQLite is used only by isolated tests; never a distributed lease.
            return
        okay = self._lease.execute(text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))"),
                                   {"key": "syn.scheduler:"+self.schedulerKey}).scalar_one()
        self._lease.commit()
        if not okay:
            self._lease.close()
            self._lease = None
            raise RuntimeError("Another ProcessScheduler owns this schedulerKey")

    def _release(self):
        if self._lease is not None:
            try:
                if self._lease.dialect.name == "postgresql":
                    self._lease.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key,0))"),
                                        {"key": "syn.scheduler:"+self.schedulerKey})
                    self._lease.commit()
            finally:
                self._lease.close()
                self._lease = None

    def _recover(self):
        with self.sessions.begin() as session:
            for job in session.scalars(self._owned().where(Job.status == JobStatus.running)):
                job.status = JobStatus.queued
                job.startedAt = None
                job.finishedAt = None
                job.error = None

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            self._acquire()
            try:
                await asyncio.to_thread(self._recover)
            except BaseException:
                self._release()
                raise
            self.running, self.fault = True, None
            self._pumpTask = asyncio.create_task(self._pump())

    async def stop(self) -> None:
        async with self._lock:
            self.running = False
            if self._pumpTask is not None:
                # Do not cancel to_thread mid-transaction. Finish the bounded DB
                # dispatch cycle before releasing ownership or disposing engines.
                await asyncio.gather(self._pumpTask, return_exceptions=True)
                self._pumpTask = None
            for scheduler in tuple(self._schedulers.values()):
                scheduler.terminate()
            if self._tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True), 15)
                except TimeoutError:
                    for task in tuple(self._tasks.values()):
                        task.cancel()
                    await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)
            self._release()

    def _due(self):
        with self.sessions.begin() as session:
            materialize_due(session, self.schedulerKey)
            rows = session.scalars(self._owned().where(Job.status == JobStatus.queued)
                .order_by(Job.createdAt).limit(self.maxConcurrentJobs)).all()
            return [row.id for row in rows]

    async def _pump(self):
        try:
            while self.running:
                # Test the exact lock-holding connection; a replacement pooled
                # connection is not evidence that this scheduler still owns it.
                self._lease.execute(text("SELECT 1"))
                self._lease.commit()
                ids = await asyncio.to_thread(self._due)
                for jobId in ids:
                    if self.running and len(self._tasks) < self.maxConcurrentJobs:
                        self._launch(jobId)
                await asyncio.sleep(.25)
        except Exception as error:
            self.fault = error
            self.running = False
            for scheduler in tuple(self._schedulers.values()):
                scheduler.terminate()
            log.exception("ProcessScheduler fenced after dispatch/ownership failure")

    def add(self, handler: str, arguments: dict[str, Any]) -> Job:
        if handler not in self.handlers:
            raise UnknownHandlerError(handler)
        with self.sessions.begin() as session:
            job = Job(id=uuid4(), handler=handler, arguments=arguments,
                      status=JobStatus.queued, createdAt=datetime.now(UTC))
            session.add(job)
            session.flush()
            bind_job(session, job.id, self.schedulerKey)
            session.expunge(job)
        return job

    def every(self, key: str, handler: str, arguments: dict[str, Any], intervalSeconds: float) -> None:
        if handler not in self.handlers:
            raise UnknownHandlerError(handler)
        with self.sessions.begin() as session:
            ensure_schedule(session, key, self.schedulerKey, handler, arguments, intervalSeconds)

    async def cancelSchedule(self, key: str) -> None:
        with self.sessions.begin() as session:
            row = session.execute(select(schedules).where(schedules.c.scheduleKey == key,
                schedules.c.schedulerKey == self.schedulerKey).with_for_update()).mappings().first()
            if row is None:
                return
            session.execute(schedules.update().where(schedules.c.scheduleKey == key).values(enabled=False))
            jobId = row["currentJobId"]
        if jobId is not None:
            await self.cancel(jobId)

    def get(self, jobId: UUID) -> Job | None:
        with self.sessions() as session:
            return session.scalars(self._owned().where(Job.id == jobId)).first()

    async def cancel(self, jobId: UUID) -> Job | None:
        with self.sessions.begin() as session:
            job = session.scalars(self._owned().where(Job.id == jobId).with_for_update(of=Job)).first()
            if job is None:
                return None
            if job.status in (JobStatus.queued, JobStatus.running):
                job.status = JobStatus.canceled
                job.finishedAt = datetime.now(UTC)
            session.flush()
            session.expunge(job)
        scheduler = self._schedulers.get(jobId)
        if scheduler is not None:
            scheduler.terminate()
        return job

    def _launch(self, jobId: UUID) -> None:
        if jobId not in self._tasks:
            task = asyncio.create_task(self._execute(jobId))
            self._tasks[jobId] = task
            def done(completed):
                self._tasks.pop(jobId, None)
                if not completed.cancelled() and completed.exception() is not None:
                    self.fault = completed.exception()
                    self.running = False
            task.add_done_callback(done)

    async def _execute(self, jobId: UUID) -> None:
        if not self.running:
            return
        with self.sessions.begin() as session:
            job = session.scalars(self._owned().where(Job.id == jobId).with_for_update(of=Job)).first()
            if job is None or job.status != JobStatus.queued:
                return
            handler = self.handlers.get(job.handler)
            if handler is None:
                job.status = JobStatus.failed
                job.error = f"Handler is no longer registered: {job.handler}"
                job.finishedAt = datetime.now(UTC)
                return
            arguments = dict(job.arguments)
            job.status = JobStatus.running
            job.startedAt = datetime.now(UTC)

        try:
            scheduler = Scheduler(raise_exceptions=True, dynamic=False, capture_stdout=True)
            self._schedulers[jobId] = scheduler
            scheduler.add(target=_invokeHandler, args=(handler, arguments))
            results = await scheduler.run()
            with self.sessions.begin() as session:
                job = session.get(Job, jobId)
                if job is not None and job.status != JobStatus.canceled:
                    job.status = JobStatus.succeeded
                    job.result = results[0]
                    job.error = None
                    job.finishedAt = datetime.now(UTC)
        except BaseException as error:
            with self.sessions.begin() as session:
                job = session.get(Job, jobId)
                if job is not None and job.status != JobStatus.canceled:
                    job.status = JobStatus.failed if self.running else JobStatus.queued
                    job.error = traceback.format_exc() if self.running else None
                    job.finishedAt = datetime.now(UTC) if self.running else None
                    if not self.running:
                        job.startedAt = None
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
        finally:
            self._schedulers.pop(jobId, None)


def _invokeHandler(handler: JobHandler, arguments: dict[str, Any]) -> Any:
    return handler(**arguments)
