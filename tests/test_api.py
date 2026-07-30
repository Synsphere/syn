from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient

from syn.api import createApp
from syn.models import Job, JobStatus


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: dict = {}
        self.running = False

    async def start(self) -> None:
        self.running = True

    def add(self, handler, arguments):
        job = Job(id=uuid4(), handler=handler, arguments=arguments,
                  status=JobStatus.queued, createdAt=datetime.now(UTC))
        self.jobs[job.id] = job
        return job

    def get(self, jobId):
        return self.jobs.get(jobId)

    async def cancel(self, jobId):
        job = self.jobs.get(jobId)
        if job:
            job.status = JobStatus.canceled
            job.finishedAt = datetime.now(UTC)
        return job


def client() -> tuple[TestClient, FakeScheduler]:
    app = createApp(Mock(), {"echo": Mock()})
    fake = FakeScheduler()
    app.state.processScheduler = fake
    return TestClient(app), fake


def test_scheduler_and_job_endpoint_flow() -> None:
    http, fake = client()
    assert http.post("/scheduler/start").json() == {"status": "running"}
    created = http.post("/jobs", json={"handler": "echo", "arguments": {"x": 3}})
    assert created.status_code == 201
    jobId = created.json()["id"]
    assert http.get(f"/jobs/{jobId}").json()["status"] == "queued"
    assert http.get(f"/jobs/{jobId}/result").status_code == 409
    canceled = http.delete(f"/jobs/{jobId}")
    assert canceled.json()["status"] == "canceled"
    assert http.get(f"/jobs/{jobId}/result").json()["status"] == "canceled"
    assert fake.running is True


def test_missing_jobs_return_not_found() -> None:
    http, _ = client()
    jobId = uuid4()
    assert http.get(f"/jobs/{jobId}").status_code == 404
    assert http.delete(f"/jobs/{jobId}").status_code == 404
    assert http.get(f"/jobs/{jobId}/result").status_code == 404
