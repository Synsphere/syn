from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from syn.api import createApp


@pytest.fixture(scope="module")
def apiClient() -> TestClient:
    return TestClient(createApp(Mock(), {}))


@pytest.mark.parametrize(
    ("path", "primitive"),
    [
        ("/api/cog", "cog"),
        ("/api/credential", "credential"),
        ("/api/ember", "ember"),
        ("/api/enigma", "enigma"),
        ("/api/event", "event"),
        ("/api/om", "om"),
        ("/api/pondle", "pondle"),
        ("/api/snug", "snug"),
        ("/api/thing", "thing"),
    ],
)
def test_primitive_get_returns_temporary_placeholder(
    apiClient: TestClient, path: str, primitive: str
) -> None:
    response = apiClient.get(path)

    assert response.status_code == 200
    assert response.json() == {
        "primitive": primitive,
        "status": "placeholder",
        "message": f"Temporary {primitive} controller response",
    }


def test_every_primitive_controller_is_published_in_openapi(apiClient: TestClient) -> None:
    schema = apiClient.get("/openapi.json").json()
    expectedPaths = {
        "/api/cog",
        "/api/credential",
        "/api/ember",
        "/api/enigma",
        "/api/event",
        "/api/om",
        "/api/pondle",
        "/api/snug",
        "/api/thing",
    }

    assert expectedPaths <= schema["paths"].keys()
    for path in expectedPaths:
        assert set(schema["paths"][path]) == {"get"}


@pytest.mark.parametrize(
    "oldPath",
    [
        "/cogs",
        "/credentials",
        "/embers",
        "/enigmas",
        "/events",
        "/oms",
        "/pondles",
        "/snugs",
        "/things",
    ],
)
def test_unprefixed_plural_primitive_routes_are_not_exposed(
    apiClient: TestClient, oldPath: str
) -> None:
    assert apiClient.get(oldPath).status_code == 404
