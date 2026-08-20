from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from syn.api import createApp
from syn.controllers.common import databaseSession as databaseSessionDependency
from syn.models import InvalidTypeParentsError, Type, TypeProperty
from syn.refs import ObjectRef


@pytest.fixture
def databaseSession() -> Mock:
    session = Mock(spec=Session)
    session.execute.return_value.all.return_value = []
    return session


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


def propertyRecord(
    owner: str = "person",
    name: str = "birthPlace",
    propertyType: str = "place",
    minimum: int = 1,
    maximum: int = 1,
) -> TypeProperty:
    return TypeProperty(
        type=ObjectRef.local("type", owner),
        name=name,
        propertyTypeRef=ObjectRef.local("type", propertyType),
        min=minimum,
        max=maximum,
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
        "parents": [],
    }
    databaseSession.get.assert_called_once_with(Type, ObjectRef("syn://type/person"))


def test_get_type_returns_recursive_parent_tree(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    person = typeRecord("person", "Person")
    socialAgent = typeRecord("social-agent", "Social agent")
    organism = typeRecord("organism", "Organism")
    agent = typeRecord("agent", "Agent")
    livingThing = typeRecord("living-thing", "Living thing")
    thing = typeRecord("thing", "thing")
    databaseSession.get.return_value = person
    databaseSession.execute.return_value.all.return_value = [
        (socialAgent, person.id),
        (organism, person.id),
        (agent, socialAgent.id),
        (livingThing, organism.id),
        (thing, agent.id),
        (thing, livingThing.id),
    ]

    response = apiClient.get("/api/type/person")

    assert response.status_code == 200
    parents = response.json()["parents"]
    assert [parent["id"] for parent in parents] == [
        "syn://type/organism",
        "syn://type/social-agent",
    ]
    assert parents[0]["parents"][0]["id"] == "syn://type/living-thing"
    assert parents[0]["parents"][0]["parents"][0]["id"] == "syn://type/thing"
    assert parents[1]["parents"][0]["id"] == "syn://type/agent"
    assert parents[1]["parents"][0]["parents"][0]["id"] == "syn://type/thing"
    statement = databaseSession.execute.call_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert 'WITH RECURSIVE "ancestorEdges"' in compiled
    assert 'FROM "typeParents"' in compiled


def test_get_type_defensively_stops_corrupt_parent_cycles(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    child = typeRecord("child", "Child")
    parent = typeRecord("parent", "Parent")
    databaseSession.get.return_value = child
    databaseSession.execute.return_value.all.return_value = [
        (parent, child.id),
        (child, parent.id),
    ]

    response = apiClient.get("/api/type/child")

    assert response.status_code == 200
    assert response.json()["parents"][0]["id"] == "syn://type/parent"
    assert response.json()["parents"][0]["parents"] == []


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
        {"name": "Person", "parents": ["https://example.com/type"]},
        {"name": "Person", "parents": ["syn://thing/not-a-type"]},
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


def test_update_type_replaces_parent_relationships(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    record = typeRecord("person", "Person")
    databaseSession.get.return_value = record
    parents = ["syn://type/human-organism", "syn://type/agent"]

    with patch.object(Type, "replaceParents") as replaceParents:
        response = apiClient.put(
            "/api/type/person",
            json={"name": "Person", "parents": parents},
        )

    assert response.status_code == 200
    replaceParents.assert_called_once_with(databaseSession, parents)
    databaseSession.commit.assert_called_once_with()


def test_update_type_reports_redundant_parent_relationships(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    record = typeRecord("person", "Person")
    databaseSession.get.return_value = record

    with patch.object(
        Type,
        "replaceParents",
        side_effect=InvalidTypeParentsError(
            "syn://type/organism is an ancestor of syn://type/human-organism"
        ),
    ):
        response = apiClient.put(
            "/api/type/person",
            json={
                "name": "Person",
                "parents": [
                    "syn://type/human-organism",
                    "syn://type/organism",
                ],
            },
        )

    assert response.status_code == 422
    assert "is an ancestor" in response.json()["detail"]
    databaseSession.rollback.assert_called_once_with()
    databaseSession.commit.assert_not_called()


def test_update_type_rolls_back_database_errors_during_parent_validation(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    databaseSession.get.return_value = typeRecord("person", "Person")

    with patch.object(
        Type,
        "replaceParents",
        side_effect=SQLAlchemyError("ancestry query failed"),
    ):
        response = apiClient.put(
            "/api/type/person",
            json={"name": "Person", "parents": ["syn://type/agent"]},
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "Unable to save type"}
    databaseSession.rollback.assert_called_once_with()
    databaseSession.commit.assert_not_called()


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


def test_get_type_properties_returns_sorted_records(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    databaseSession.get.return_value = typeRecord("person", "Person")
    databaseSession.scalars.return_value.all.return_value = [
        propertyRecord(name="birthPlace"),
        propertyRecord(name="name", propertyType="text-value", minimum=0, maximum=3),
    ]

    response = apiClient.get("/api/type/person/properties")

    assert response.status_code == 200
    assert response.json() == [
        {
            "type": "syn://type/person",
            "name": "birthPlace",
            "propertyTypeRef": "syn://type/place",
            "min": 1,
            "max": 1,
        },
        {
            "type": "syn://type/person",
            "name": "name",
            "propertyTypeRef": "syn://type/text-value",
            "min": 0,
            "max": 3,
        },
    ]
    statement = databaseSession.scalars.call_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert 'FROM "typeProperties"' in compiled
    assert 'ORDER BY "typeProperties".name' in compiled


def test_create_type_property(apiClient: TestClient, databaseSession: Mock) -> None:
    databaseSession.get.side_effect = [typeRecord("person", "Person"), None]

    response = apiClient.post(
        "/api/type/person/properties",
        json={
            "name": "  birthPlace  ",
            "propertyTypeRef": "syn://type/place",
            "min": 0,
            "max": 1,
        },
    )

    assert response.status_code == 201
    assert response.json() == {
        "type": "syn://type/person",
        "name": "birthPlace",
        "propertyTypeRef": "syn://type/place",
        "min": 0,
        "max": 1,
    }
    created = databaseSession.add.call_args.args[0]
    assert isinstance(created, TypeProperty)
    assert created.type == "syn://type/person"
    databaseSession.commit.assert_called_once_with()


def test_create_type_property_rejects_duplicate(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    existing = propertyRecord()
    databaseSession.get.side_effect = [typeRecord("person", "Person"), existing]

    response = apiClient.post(
        "/api/type/person/properties",
        json={"name": "birthPlace", "propertyTypeRef": "syn://type/place"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Type property already exists"}
    databaseSession.add.assert_not_called()


def test_update_type_property_can_rename_and_change_cardinality(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    existing = propertyRecord()
    databaseSession.get.side_effect = [typeRecord("person", "Person"), existing, None]

    response = apiClient.put(
        "/api/type/person/property/birthPlace",
        json={
            "name": "placeOfBirth",
            "propertyTypeRef": "syn://type/geographic-region",
            "min": 0,
            "max": 2,
        },
    )

    assert response.status_code == 200
    assert response.json()["name"] == "placeOfBirth"
    assert response.json()["propertyTypeRef"] == "syn://type/geographic-region"
    assert existing.min == 0
    assert existing.max == 2
    databaseSession.commit.assert_called_once_with()


def test_delete_type_property(apiClient: TestClient, databaseSession: Mock) -> None:
    existing = propertyRecord(name="display name")
    databaseSession.get.side_effect = [typeRecord("person", "Person"), existing]

    response = apiClient.delete("/api/type/person/property/display%20name")

    assert response.status_code == 204
    assert response.content == b""
    databaseSession.delete.assert_called_once_with(existing)
    databaseSession.commit.assert_called_once_with()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": "", "propertyTypeRef": "syn://type/place"},
        {"name": "x" * 1025, "propertyTypeRef": "syn://type/place"},
        {"name": "place", "propertyTypeRef": "https://example.com/place"},
        {"name": "place", "propertyTypeRef": "syn://thing/place"},
        {"name": "place", "propertyTypeRef": "syn://type/place", "min": -1},
        {"name": "place", "propertyTypeRef": "syn://type/place", "min": 2, "max": 1},
        {"name": "place", "propertyTypeRef": "syn://type/place", "extra": True},
    ],
)
def test_type_property_writes_reject_invalid_bodies(
    apiClient: TestClient, databaseSession: Mock, body: dict
) -> None:
    assert apiClient.post("/api/type/person/properties", json=body).status_code == 422
    assert apiClient.put("/api/type/person/property/place", json=body).status_code == 422
    databaseSession.get.assert_not_called()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/type/missing/properties"),
        ("post", "/api/type/missing/properties"),
        ("put", "/api/type/missing/property/name"),
        ("delete", "/api/type/missing/property/name"),
    ],
)
def test_property_routes_require_existing_type(
    apiClient: TestClient, databaseSession: Mock, method: str, path: str
) -> None:
    databaseSession.get.return_value = None
    request = getattr(apiClient, method)
    keyword = {} if method in {"get", "delete"} else {
        "json": {"name": "name", "propertyTypeRef": "syn://type/thing"}
    }

    response = request(path, **keyword)

    assert response.status_code == 404
    assert response.json() == {"detail": "Type not found"}


@pytest.mark.parametrize(
    "primitive",
    ["cog", "credential", "ember", "enigma", "event", "om", "pondle", "snug", "thing"],
)
def test_get_type_tree_can_start_at_every_primitive(
    apiClient: TestClient,
    databaseSession: Mock,
    primitive: str,
) -> None:
    databaseSession.get.return_value = typeRecord(primitive, primitive.title())

    response = apiClient.get(f"/api/type/{primitive}/tree")

    assert response.status_code == 200
    assert response.json() == {
        "typeRef": f"syn://type/{primitive}",
        "name": primitive.title(),
        "children": [],
    }
    databaseSession.get.assert_called_once_with(
        Type,
        ObjectRef(f"syn://type/{primitive}"),
    )


def test_get_type_tree_returns_recursive_sorted_children(
    apiClient: TestClient,
    databaseSession: Mock,
) -> None:
    thing = typeRecord("thing", "Thing")
    agent = typeRecord("agent", "Agent")
    objectType = typeRecord("object", "Object")
    person = typeRecord("person", "Person")
    databaseSession.get.return_value = thing
    databaseSession.execute.return_value.all.return_value = [
        (objectType, thing.id),
        (person, agent.id),
        (agent, thing.id),
    ]

    response = apiClient.get("/api/type/thing/tree", params={"depth": 2})

    assert response.status_code == 200
    assert response.json() == {
        "typeRef": "syn://type/thing",
        "name": "Thing",
        "children": [
            {
                "typeRef": "syn://type/agent",
                "name": "Agent",
                "children": [
                    {
                        "typeRef": "syn://type/person",
                        "name": "Person",
                        "children": [],
                    }
                ],
            },
            {
                "typeRef": "syn://type/object",
                "name": "Object",
                "children": [],
            },
        ],
    }
    statement = databaseSession.execute.call_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert 'WITH RECURSIVE "descendantEdges"' in compiled
    assert 'FROM "typeParents"' in compiled
    assert '"typeParents"."parentRef"' in compiled


def test_get_type_tree_depth_zero_returns_only_the_root(
    apiClient: TestClient,
    databaseSession: Mock,
) -> None:
    databaseSession.get.return_value = typeRecord("thing", "Thing")

    response = apiClient.get("/api/type/thing/tree", params={"depth": 0})

    assert response.status_code == 200
    assert response.json()["children"] == []
    databaseSession.execute.assert_not_called()


@pytest.mark.parametrize("depth", [-1, 101, 1.5, "many"])
def test_get_type_tree_rejects_invalid_depth(
    apiClient: TestClient,
    databaseSession: Mock,
    depth: object,
) -> None:
    response = apiClient.get("/api/type/thing/tree", params={"depth": depth})

    assert response.status_code == 422
    databaseSession.get.assert_not_called()


def test_get_type_tree_returns_not_found(
    apiClient: TestClient,
    databaseSession: Mock,
) -> None:
    databaseSession.get.return_value = None

    response = apiClient.get("/api/type/missing/tree")

    assert response.status_code == 404
    assert response.json() == {"detail": "Type not found"}


def test_get_type_tree_defensively_stops_corrupt_child_cycles(
    apiClient: TestClient,
    databaseSession: Mock,
) -> None:
    child = typeRecord("child", "Child")
    parent = typeRecord("parent", "Parent")
    databaseSession.get.return_value = parent
    databaseSession.execute.return_value.all.return_value = [
        (child, parent.id),
        (parent, child.id),
    ]

    response = apiClient.get("/api/type/parent/tree", params={"depth": 100})

    assert response.status_code == 200
    assert response.json()["children"][0]["typeRef"] == "syn://type/child"
    assert response.json()["children"][0]["children"] == []


def test_property_routes_report_missing_property(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    databaseSession.get.side_effect = [typeRecord("person", "Person"), None]

    response = apiClient.delete("/api/type/person/property/missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Type property not found"}


def test_create_type_property_rolls_back_database_errors(
    apiClient: TestClient, databaseSession: Mock
) -> None:
    databaseSession.get.side_effect = [typeRecord("person", "Person"), None]
    databaseSession.commit.side_effect = SQLAlchemyError("rejected")

    response = apiClient.post(
        "/api/type/person/properties",
        json={"name": "place", "propertyTypeRef": "syn://type/place"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Unable to create type property"}
    databaseSession.rollback.assert_called_once_with()


def test_type_routes_are_published_in_openapi(apiClient: TestClient) -> None:
    paths = apiClient.get("/openapi.json").json()["paths"]

    assert set(paths["/api/type"]) == {"get"}
    assert set(paths["/api/type/{id}"]) == {"get", "put"}
    assert set(paths["/api/type/{id}/tree"]) == {"get"}
    assert set(paths["/api/type/{id}/properties"]) == {"get", "post"}
    assert set(paths["/api/type/{id}/property/{name}"]) == {"put", "delete"}
    depthParameter = next(
        parameter
        for parameter in paths["/api/type/{id}/tree"]["get"]["parameters"]
        if parameter["name"] == "depth"
    )
    assert depthParameter["schema"]["default"] == 10
    assert depthParameter["schema"]["minimum"] == 0
    assert depthParameter["schema"]["maximum"] == 100
