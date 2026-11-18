from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Callable, Mapping
from contextvars import copy_context
from datetime import UTC, datetime, timedelta
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


async def _run_owned_thread(function: Callable[..., Any], *arguments: Any) -> Any:
    """Join startup work before cancellation can release its database lease.

    Canceling an asyncio await does not stop its worker thread. Keep the worker
    shielded and join it even if shutdown sends more than one cancellation.
    The caller still receives cancellation after the operation has finished.
    """
    # Use a Future instead of a child Task: global asyncio shutdown cancels
    # every Task directly, which would bypass shield and abandon to_thread.
    worker = asyncio.get_running_loop().run_in_executor(
        None, copy_context().run, function, *arguments)
    canceled = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as error:
            if canceled is None:
                canceled = error
        except BaseException:
            # The worker's error is retrieved below, also when cancellation
            # wins, so no failed background task is abandoned or unobserved.
            break
    if canceled is not None:
        try:
            worker.result()
        except BaseException as error:
            raise canceled from error
        raise canceled
    return worker.result()


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
        self._preparing = False

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

    async def start(self, *, prepare=None) -> None:
        async with self._lock:
            if self.running:
                return
            self._acquire()
            try:
                if prepare is not None:
                    self._preparing = True
                    try:
                        await _run_owned_thread(prepare, self)
                    finally:
                        self._preparing = False
                await _run_owned_thread(self._recover)
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

    def reconcile_startup_schedule(self, key: str, handler: str,
                                   arguments: dict[str, Any], intervalSeconds: float) -> Any:
        """Run and audit one schedule before dispatch starts, recovering its marker.

        Only a successful current execution can supersede that schedule's old
        failure. Historical failed Jobs remain intact. Pending Jobs for this
        exact schedule are canceled after success so startup's verified result
        cannot immediately be followed by duplicate recovered work.
        """
        if not self._preparing or self._lease is None or self.running:
            raise RuntimeError("Startup reconciliation requires scheduler preparation ownership")
        lease = self._lease
        if handler not in self.handlers:
            raise UnknownHandlerError(handler)
        # Validate ownership before invoking any authority-bearing handler. Do
        # not hold a database transaction across a native approval/signing call.
        with self.sessions.begin() as session:
            ensure_schedule(session, key, self.schedulerKey, handler, arguments, intervalSeconds)
            row = session.execute(select(schedules).where(
                schedules.c.scheduleKey == key).with_for_update()).mappings().one()
            if row["currentJobId"] is not None:
                job = session.get(Job, row["currentJobId"])
                binding = session.execute(select(dispatch).where(
                    dispatch.c.jobId == row["currentJobId"])).mappings().first()
                if (job is None or binding is None or binding["schedulerKey"] != self.schedulerKey
                        or binding["scheduleKey"] != key or job.handler != handler):
                    raise RuntimeError("Startup schedule has an inconsistent current Job binding")
        started = datetime.now(UTC)
        result = self.handlers[handler](**arguments)
        if not self._preparing or self._lease is not lease or self.running:
            raise RuntimeError("Startup preparation ended before reconciliation completed")
        finished, completed_id = datetime.now(UTC), uuid4()
        with self.sessions.begin() as session:
            row = session.execute(select(schedules).where(
                schedules.c.scheduleKey == key).with_for_update()).mappings().one()
            if row["schedulerKey"] != self.schedulerKey or row["handler"] != handler:
                raise RuntimeError("Startup schedule ownership changed during reconciliation")
            pending = session.scalars(select(Job).join(dispatch, dispatch.c.jobId == Job.id).where(
                dispatch.c.scheduleKey == key, dispatch.c.schedulerKey == self.schedulerKey,
                Job.status.in_((JobStatus.queued, JobStatus.running))).with_for_update(of=Job)).all()
            for job in pending:
                job.status = JobStatus.canceled
                job.finishedAt = finished
                job.error = f"Superseded by verified startup Job {completed_id}"
            completed = Job(id=completed_id, handler=handler, arguments=dict(arguments),
                status=JobStatus.succeeded, result=result, createdAt=started,
                startedAt=started, finishedAt=finished)
            session.add(completed)
            session.flush()
            bind_job(session, completed_id, self.schedulerKey, key)
            session.execute(schedules.update().where(schedules.c.scheduleKey == key).values(
                currentJobId=completed_id, failures=0,
                nextRunAt=finished + timedelta(seconds=intervalSeconds)))
        return result

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
