from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from .controllers import primitiveRouters
from .models import Job, JobStatus
from .config import P2PConfig
from .p2p import Libp2pNode
from .scheduler import JobHandler, ProcessScheduler, UnknownHandlerError


templates = Jinja2Templates(directory=Path(__file__).with_name("templates"))


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
    sessions: sessionmaker[Session], handlers: dict[str, JobHandler],
    p2pConfig: P2PConfig | None = None,
    *, communicationTokens: Mapping[str, str] | None = None,
    positionRuntime=None,
    operatorRuntime=None,
    witnessConfig=None,
    cardanoConfig=None,
    bluetoothConfig=None,
) -> FastAPI:
    from .positions.types import digest
    scheduler = ProcessScheduler(sessions, handlers,
        schedulerKey="node-"+digest(positionRuntime.nodeRef) if positionRuntime else "default",
        maxConcurrentJobs=positionRuntime.config.maxConcurrentJobs if positionRuntime else 4)
    p2pNode = Libp2pNode(p2pConfig or P2PConfig(),
                        operatorRuntime.server if operatorRuntime is not None else None)
    from .cardano.runtime import CardanoRuntime
    cardano = CardanoRuntime(sessions, positionRuntime.nodeRef if positionRuntime else None,
                            cardanoConfig)
    from .bluetooth.runtime import BluetoothRuntime
    bluetooth = BluetoothRuntime(sessions, positionRuntime.nodeRef if positionRuntime else None,
                                 bluetoothConfig)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from starlette.concurrency import run_in_threadpool
        watchdog = None
        app.state.awarenessFault = None
        async def supervise():
            while True:
                await asyncio.sleep(1)
                try:
                    if scheduler.fault is not None:
                        raise RuntimeError("Node scheduler lost readiness") from scheduler.fault
                    p2pNode.check()
                    cardano.check()
                    bluetooth.check()
                    if operatorRuntime is not None:
                        operatorRuntime.check()
                        if "operator-dag-sync" in handlers:
                            from .witnessing.runtime import check_local_jobs
                            await run_in_threadpool(check_local_jobs, sessions, scheduler.schedulerKey)
                except Exception as error:
                    app.state.awarenessFault = error
                    logging.getLogger("uvicorn.error").error("Syn node lost readiness; API is fenced")
                    await bluetooth.stop()
                    await cardano.stop()
                    await p2pNode.stop()
                    await scheduler.stop()
                    return
        try:
            # Durable recurring database work uses the node scheduler and lease.
            def prepare(manager):
                if positionRuntime is not None:
                    positionRuntime.prepare(manager)
                if operatorRuntime is not None and "operator-dag-sync" in handlers:
                    handlers["operator-dag-sync"](schedulerKey=manager.schedulerKey)
                    manager.every("operator-dag-" + operatorRuntime.server.expected_hash,
                                  "operator-dag-sync", {"schedulerKey": manager.schedulerKey}, 120)
                if operatorRuntime is not None and positionRuntime is not None and witnessConfig is not None:
                    from .witnessing.runtime import prepare as prepare_witnessing
                    prepare_witnessing(sessions, operatorRuntime.server, positionRuntime.nodeRef,
                                      witnessConfig, manager)

            # Acquire native authority first; reconcile DAG once inside scheduler
            # preparation, before its pump can materialize a duplicate sync job.
            if operatorRuntime is not None:
                await run_in_threadpool(operatorRuntime.start)
            if positionRuntime is not None:
                await scheduler.start(prepare=prepare)
            await p2pNode.start()
            await cardano.start()
            await bluetooth.start()
            watchdog = asyncio.create_task(supervise())
            yield
        finally:
            if watchdog is not None:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
            try:
                await bluetooth.stop()
            finally:
                try:
                    await cardano.stop()
                finally:
                    try:
                        await p2pNode.stop()
                    finally:
                        try:
                            if operatorRuntime is not None:
                                await run_in_threadpool(operatorRuntime.close)
                        finally:
                            await scheduler.stop()

    app = FastAPI(title="Syn API", lifespan=lifespan)
    app.state.processScheduler = scheduler
    app.state.sessions = sessions
    app.state.p2pNode = p2pNode
    app.state.cardano = cardano
    app.state.bluetooth = bluetooth
    app.state.awarenessFault = None

    @app.middleware("http")
    async def readiness_fence(request, call_next):
        if app.state.awarenessFault is not None and request.url.path != "/bluetooth/status":
            return JSONResponse({"detail": "Node awareness, Operator, scheduler or peer transport is not ready"}, status_code=503)
        return await call_next(request)

    from .communication import communicationRouter, communicationTokens as loadCommunicationTokens
    tokens = loadCommunicationTokens() if communicationTokens is None else communicationTokens
    if tokens:
        app.include_router(communicationRouter(sessions, tokens))
    from .public_pubsub_http import build_router as build_public_pubsub_router
    app.include_router(build_public_pubsub_router())
    if operatorRuntime is not None:
        from .operator.dag_http import build_router
        app.include_router(build_router(sessions, operatorRuntime.server.expected_hash))
        if witnessConfig is not None and positionRuntime is not None:
            from .witnessing.http import build_router as build_witness_router
            app.include_router(build_witness_router(sessions, operatorRuntime.server.expected_hash,
                               positionRuntime.nodeRef, witnessConfig))

    @app.get("/v1/capabilities")
    async def capabilities() -> dict[str, Any]:
        from .release import runtimeContract
        return runtimeContract().to_dict()

    @app.get("/p2p/status")
    async def p2pStatus(request: Request) -> dict[str, Any]:
        return request.app.state.p2pNode.status()

    @app.get("/cardano/status")
    async def cardanoStatus(request: Request) -> dict[str, Any]:
        """Report independent trusted-node followers and durable checkpoints."""
        return request.app.state.cardano.status()

    @app.get("/bluetooth/status")
    async def bluetoothStatus(request: Request) -> dict[str, Any]:
        """Report native adapters, half-duplex activity and observation accounting."""
        return request.app.state.bluetooth.status()

    @app.get("/p2p/peer-record")
    async def p2pPeerRecord(request: Request) -> dict[str, Any]:
        packet = request.app.state.p2pNode.public_record()
        if packet is None:
            raise HTTPException(409, "A current signed Operator peer record is not available")
        return packet

    @app.get("/p2p/operators/{operator_hash}")
    async def p2pOperator(operator_hash: str, request: Request, network: str = "syn") -> dict[str, Any]:
        from .p2p import DiscoveryBusy, DiscoveryUnavailable
        try:
            peers = await request.app.state.p2pNode.find_operator(operator_hash, network)
        except ValueError:
            raise HTTPException(422, "Expected a lowercase Operator base hash and valid network") from None
        except DiscoveryBusy:
            raise HTTPException(429, "Operator discovery is at capacity") from None
        except DiscoveryUnavailable:
            raise HTTPException(503, "Operator discovery is temporarily unavailable") from None
        return {"operator": operator_hash, "network": network, "peers": peers}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name="index.html")

    @app.get("/editTypes", response_class=HTMLResponse, include_in_schema=False)
    async def editTypes(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name="editTypes.html")

    @app.get("/exploreTypes", response_class=HTMLResponse, include_in_schema=False)
    async def exploreTypes(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name="exploreTypes.html")

    for router in primitiveRouters:
        app.include_router(router, prefix="/api")

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
