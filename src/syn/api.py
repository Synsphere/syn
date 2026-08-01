from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from .controllers import primitiveRouters
from .models import Job, JobStatus
from .scheduler import JobHandler, ProcessScheduler, UnknownHandlerError


class AddJobRequest(BaseModel):
    handler: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    handler: str
    arguments: dict[str, Any]
    status: JobStatus
    error: str | None
    createdAt: datetime
    startedAt: datetime | None
    finishedAt: datetime | None


class JobResultResponse(BaseModel):
    id: UUID
    status: JobStatus
    result: Any | None = None
    error: str | None = None


def createApp(
    sessions: sessionmaker[Session], handlers: dict[str, JobHandler]
) -> FastAPI:
    scheduler = ProcessScheduler(sessions, handlers)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await scheduler.stop()

    app = FastAPI(title="Syn API", lifespan=lifespan)
    app.state.processScheduler = scheduler

    for router in primitiveRouters:
        app.include_router(router)

    def service(request: Request) -> ProcessScheduler:
        return request.app.state.processScheduler

    @app.post("/scheduler/start", status_code=status.HTTP_202_ACCEPTED)
    async def startScheduler(manager: ProcessScheduler = Depends(service)) -> dict[str, str]:
        await manager.start()
        return {"status": "running"}

    @app.post("/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
    async def addJob(body: AddJobRequest, manager: ProcessScheduler = Depends(service)) -> Job:
        try:
            return manager.add(body.handler, body.arguments)
        except UnknownHandlerError as error:
            raise HTTPException(status_code=422, detail=f"Unknown handler: {error}") from error

    @app.get("/jobs/{jobId}", response_model=JobResponse)
    async def getJob(jobId: UUID, manager: ProcessScheduler = Depends(service)) -> Job:
        job = manager.get(jobId)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @app.delete("/jobs/{jobId}", response_model=JobResponse)
    async def cancelJob(jobId: UUID, manager: ProcessScheduler = Depends(service)) -> Job:
        job = await manager.cancel(jobId)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @app.get("/jobs/{jobId}/result", response_model=JobResultResponse)
    async def getResult(jobId: UUID, manager: ProcessScheduler = Depends(service)) -> JobResultResponse:
        job = manager.get(jobId)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status in (JobStatus.queued, JobStatus.running):
            raise HTTPException(status_code=409, detail="Job has not finished")
        return JobResultResponse(id=job.id, status=job.status, result=job.result, error=job.error)

    return app
