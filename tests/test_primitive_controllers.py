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
        ("/cogs", "cog"),
        ("/embers", "ember"),
        ("/enigmas", "enigma"),
        ("/events", "event"),
        ("/oms", "om"),
        ("/pondles", "pondle"),
        ("/snugs", "snug"),
        ("/things", "thing"),
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
        "/cogs",
        "/embers",
        "/enigmas",
        "/events",
        "/oms",
        "/pondles",
        "/snugs",
        "/things",
    }

    assert expectedPaths <= schema["paths"].keys()
    for path in expectedPaths:
        assert set(schema["paths"][path]) == {"get"}
