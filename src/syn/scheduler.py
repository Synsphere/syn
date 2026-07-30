from __future__ import annotations

import asyncio
import traceback
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from scheduler.Scheduler import Scheduler
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import Job, JobStatus

JobHandler = Callable[..., Any]


class SchedulerNotRunningError(RuntimeError):
    pass


class UnknownHandlerError(ValueError):
    pass


class ProcessScheduler:
    """Coordinates persistent jobs executed by AsyncProcessScheduler."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        handlers: Mapping[str, JobHandler],
    ) -> None:
        self.sessions = sessions
        self.handlers = dict(handlers)
        self.running = False
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._schedulers: dict[UUID, Scheduler] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            self.running = True
            with self.sessions.begin() as session:
                jobs = session.scalars(
                    select(Job).where(Job.status.in_([JobStatus.queued, JobStatus.running]))
                ).all()
                for job in jobs:
                    job.status = JobStatus.queued
                    job.startedAt = None
                    self._launch(job.id)

    async def stop(self) -> None:
        self.running = False
        for scheduler in tuple(self._schedulers.values()):
            scheduler.terminate()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)

    def add(self, handler: str, arguments: dict[str, Any]) -> Job:
        if handler not in self.handlers:
            raise UnknownHandlerError(handler)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            job = Job(
                id=uuid4(), handler=handler, arguments=arguments,
                status=JobStatus.queued, createdAt=now,
            )
            session.add(job)
        if self.running:
            self._launch(job.id)
        return job

    def get(self, jobId: UUID) -> Job | None:
        with self.sessions() as session:
            return session.get(Job, jobId)

    async def cancel(self, jobId: UUID) -> Job | None:
        scheduler = self._schedulers.get(jobId)
        if scheduler is not None:
            scheduler.terminate()
        with self.sessions.begin() as session:
            job = session.get(Job, jobId)
            if job is None:
                return None
            if job.status in (JobStatus.queued, JobStatus.running):
                job.status = JobStatus.canceled
                job.finishedAt = datetime.now(UTC)
            return job

    def _launch(self, jobId: UUID) -> None:
        if jobId not in self._tasks:
            task = asyncio.create_task(self._execute(jobId))
            self._tasks[jobId] = task
            task.add_done_callback(lambda _: self._tasks.pop(jobId, None))

    async def _execute(self, jobId: UUID) -> None:
        with self.sessions.begin() as session:
            job = session.get(Job, jobId)
            if job is None or job.status is JobStatus.canceled:
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

        scheduler = Scheduler(raise_exceptions=True, dynamic=False, capture_stdout=True)
        self._schedulers[jobId] = scheduler
        scheduler.add(target=_invokeHandler, args=(handler, arguments))
        try:
            results = await scheduler.run()
            with self.sessions.begin() as session:
                job = session.get(Job, jobId)
                if job is not None and job.status is not JobStatus.canceled:
                    job.status = JobStatus.succeeded
                    job.result = results[0]
                    job.finishedAt = datetime.now(UTC)
        except Exception:
            with self.sessions.begin() as session:
                job = session.get(Job, jobId)
                if job is not None and job.status is not JobStatus.canceled:
                    job.status = JobStatus.failed
                    job.error = traceback.format_exc()
                    job.finishedAt = datetime.now(UTC)
        finally:
            self._schedulers.pop(jobId, None)


def _invokeHandler(handler: JobHandler, arguments: dict[str, Any]) -> Any:
    return handler(**arguments)
