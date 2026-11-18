from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from syn.api import createApp
from syn.models import Job, JobStatus
from syn.p2p import DiscoveryBusy, DiscoveryUnavailable


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


def test_index_renders_welcome_template() -> None:
    http, _ = client()

    response = http.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<h1>Welcome to Syn</h1>" in response.text


def test_index_is_not_published_as_an_api_operation() -> None:
    http, _ = client()

    assert "/" not in http.get("/openapi.json").json()["paths"]


def test_p2p_status_is_accessible() -> None:
    http, _ = client()

    response = http.get("/p2p/status")

    assert response.status_code == 200
    assert response.json()["enabled"] is True


def test_unavailable_local_peer_record_is_distinct_from_failed_discovery() -> None:
    http, _ = client()
    assert http.get("/p2p/peer-record").status_code == 409
    assert http.get("/p2p/operators/" + "a" * 64).status_code == 503


@pytest.mark.parametrize("path", [
    "/p2p/operators/not-a-hash",
    "/p2p/operators/" + "A" * 64,
    "/p2p/operators/" + "a" * 64 + "?network=Syn",
])
def test_discovery_rejects_invalid_identity_before_attempting_network(path) -> None:
    http, _ = client()
    assert http.get(path).status_code == 422


@pytest.mark.parametrize("error,expected", [
    (DiscoveryBusy("internal capacity detail"), 429),
    (DiscoveryUnavailable("internal transport detail"), 503),
])
def test_discovery_api_reports_capacity_and_outage_separately(error, expected) -> None:
    http, _ = client()
    http.app.state.p2pNode.find_operator = AsyncMock(side_effect=error)
    response = http.get("/p2p/operators/" + "a" * 64)
    assert response.status_code == expected
    assert "internal" not in response.text


def test_discovery_api_preserves_verified_packets_and_requested_network() -> None:
    http, _ = client()
    packet = {"body": {"operator": "a" * 64, "network_id": "syn-test"},
              "public_key": "base-public-key", "signature": "base-signature"}
    node = http.app.state.p2pNode
    node.find_operator = AsyncMock(return_value=[packet])
    node.public_record = Mock(return_value=packet)
    response = http.get("/p2p/operators/" + "a" * 64 + "?network=syn-test")
    assert response.status_code == 200
    assert response.json() == {"operator": "a" * 64, "network": "syn-test", "peers": [packet]}
    node.find_operator.assert_awaited_once_with("a" * 64, "syn-test")
    assert http.get("/p2p/peer-record").json() == packet


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
