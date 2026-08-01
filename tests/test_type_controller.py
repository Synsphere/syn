from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from syn.api import createApp
from syn.controllers.common import databaseSession as databaseSessionDependency
from syn.models import Type
from syn.refs import ObjectRef


@pytest.fixture
def databaseSession() -> Mock:
    return Mock(spec=Session)


@pytest.fixture
def apiClient(databaseSession: Mock) -> TestClient:
    app = createApp(Mock(), {})

    def overrideDatabaseSession():
        yield databaseSession

    app.dependency_overrides[databaseSessionDependency] = overrideDatabaseSession
    return TestClient(app)


def typeRecord(slug: str, name: str) -> Type:
    return Type(
        id=ObjectRef.local("type", slug),
        name=name,
        wikipediaRef=f"https://en.wikipedia.org/wiki/{slug}",
        locality=1,
        knowledge={"description": f"The {name} type."},
        confidence=Decimal("0.75"),
    )


def test_get_type_resolves_local_object_reference(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    databaseSession.get.return_value = typeRecord("person", "Person")

    response = apiClient.get("/api/type/person")

    assert response.status_code == 200
    assert response.json() == {
        "id": "syn://type/person",
        "name": "Person",
        "wikipediaRef": "https://en.wikipedia.org/wiki/person",
        "locality": 1,
        "stats": None,
        "statsUpdated": None,
        "knowledge": {"description": "The Person type."},
        "knowledgeUpdated": None,
        "confidence": "0.75",
        "confidenceUpdated": None,
    }
    databaseSession.get.assert_called_once_with(Type, ObjectRef("syn://type/person"))


def test_get_type_returns_not_found(apiClient: TestClient, databaseSession: Mock) -> None:
    databaseSession.get.return_value = None

    response = apiClient.get("/api/type/missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Type not found"}


def test_update_type_saves_existing_record_and_updates_changed_timestamps(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    previousUpdate = datetime(2025, 1, 1, tzinfo=UTC)
    record = typeRecord("person", "Person")
    record.stats = {"count": 1}
    record.statsUpdated = previousUpdate
    record.knowledgeUpdated = previousUpdate
    record.confidenceUpdated = previousUpdate
    databaseSession.get.return_value = record

    response = apiClient.put(
        "/api/type/person",
        json={
            "name": "  Human person  ",
            "wikipediaRef": None,
            "locality": 2,
            "stats": {"count": 2},
            "knowledge": record.knowledge,
            "confidence": "0.5",
        },
    )

    assert response.status_code == 200
    assert response.json()["id"] == "syn://type/person"
    assert response.json()["name"] == "Human person"
    assert record.wikipediaRef is None
    assert record.locality == 2
    assert record.stats == {"count": 2}
    assert record.statsUpdated > previousUpdate
    assert record.knowledgeUpdated == previousUpdate
    assert record.confidence == Decimal("0.5")
    assert record.confidenceUpdated > previousUpdate
    databaseSession.get.assert_called_once_with(Type, ObjectRef("syn://type/person"))
    databaseSession.commit.assert_called_once_with()
    databaseSession.rollback.assert_not_called()


def test_update_type_returns_not_found_without_committing(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    databaseSession.get.return_value = None

    response = apiClient.put("/api/type/missing", json={"name": "Missing"})

    assert response.status_code == 404
    databaseSession.commit.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "Person", "confidence": -0.01},
        {"name": "Person", "confidence": 1.01},
        {"name": "Person", "id": "syn://type/other"},
        {"name": "Person", "stats": []},
        {"name": "Person", "knowledge": "not-json"},
    ],
)
def test_update_type_rejects_invalid_bodies(
    apiClient: TestClient, databaseSession: Mock, body: dict
) -> None:
    response = apiClient.put("/api/type/person", json=body)

    assert response.status_code == 422
    databaseSession.get.assert_not_called()
    databaseSession.commit.assert_not_called()


def test_update_type_rolls_back_database_errors(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    databaseSession.get.return_value = typeRecord("person", "Person")
    databaseSession.commit.side_effect = SQLAlchemyError("database rejected update")

    response = apiClient.put("/api/type/person", json={"name": "Updated"})

    assert response.status_code == 409
    assert response.json() == {"detail": "Unable to save type"}
    databaseSession.rollback.assert_called_once_with()


def test_type_controller_closes_its_database_session() -> None:
    session = Mock(spec=Session)
    session.get.return_value = None
    sessions = Mock(return_value=session)
    http = TestClient(createApp(sessions, {}))

    response = http.get("/api/type/missing")

    assert response.status_code == 404
    sessions.assert_called_once_with()
    session.close.assert_called_once_with()


@pytest.mark.parametrize("typeId", ["Person", "bad.slug", "-leading", "trailing-"])
def test_get_type_rejects_invalid_slugs(apiClient: TestClient, typeId: str) -> None:
    assert apiClient.get(f"/api/type/{typeId}").status_code == 422


@pytest.mark.parametrize("typeId", ["Person", "bad.slug", "-leading", "trailing-"])
def test_update_type_rejects_invalid_slugs(apiClient: TestClient, typeId: str) -> None:
    assert apiClient.put(f"/api/type/{typeId}", json={"name": "Person"}).status_code == 422


def test_search_types_returns_serialized_records(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    databaseSession.scalars.return_value.all.return_value = [
        typeRecord("person", "Person"),
        typeRecord("fictional-person", "Fictional person"),
    ]

    response = apiClient.get("/api/type", params={"query": "person"})

    assert response.status_code == 200
    assert [record["id"] for record in response.json()] == [
        "syn://type/person",
        "syn://type/fictional-person",
    ]
    statement = databaseSession.scalars.call_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert 'FROM types' in compiled
    assert 'types.name ILIKE' in compiled
    assert 'CAST(types.id AS TEXT) ILIKE' in compiled
    assert 'ORDER BY CASE' in compiled
    assert statement._limit_clause.value == 10
    assert "syn://type/person" in statement.compile().params.values()


def test_search_types_never_returns_more_than_ten_records(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    databaseSession.scalars.return_value.all.return_value = [
        typeRecord(f"result-{index}", f"Result {index}") for index in range(11)
    ]

    response = apiClient.get("/api/type", params={"query": "result"})

    assert response.status_code == 200
    assert len(response.json()) == 10


def test_search_treats_sql_wildcards_as_literal_characters(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    databaseSession.scalars.return_value.all.return_value = []

    response = apiClient.get("/api/type", params={"query": r"100%_type"})

    assert response.status_code == 200
    statement = databaseSession.scalars.call_args.args[0]
    assert r"%100\%\_type%" in statement.compile().params.values()


@pytest.mark.parametrize("query", [None, "", "   "])
def test_search_types_requires_non_blank_query(
    apiClient: TestClient, databaseSession: Mock, query: str | None
) -> None:
    parameters = {} if query is None else {"query": query}

    response = apiClient.get("/api/type", params=parameters)

    assert response.status_code == 422
    databaseSession.scalars.assert_not_called()


def test_search_rejects_queries_over_255_characters(apiClient: TestClient) -> None:
    assert apiClient.get("/api/type", params={"query": "x" * 256}).status_code == 422


def test_type_routes_are_published_in_openapi(apiClient: TestClient) -> None:
    paths = apiClient.get("/openapi.json").json()["paths"]

    assert set(paths["/api/type"]) == {"get"}
    assert set(paths["/api/type/{id}"]) == {"get", "put"}
