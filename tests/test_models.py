from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from syn import (
    Base,
    ChronicleKind,
    Cog,
    Ember,
    Enigma,
    Event,
    EventTarget,
    Job,
    JobStatus,
    ObjectChronicle,
    ObjectRef,
    ObjectRegistry,
    ObjectRelationship,
    Om,
    Pondle,
    Snug,
    Thing,
    Type,
    TypeParent,
    TypeProperty,
    modelByObjectTable,
)


MODEL_CLASSES = [Type, Thing, Event, Ember, Cog, Enigma, Pondle, Snug, Om]


def test_every_schema_table_has_exactly_one_model() -> None:
    assert set(Base.metadata.tables) == {
        "objectRegistry",
        "types",
        "things",
        "events",
        "embers",
        "cogs",
        "enigmas",
        "pondles",
        "snugs",
        "oms",
        "typeParents",
        "typeProperties",
        "eventTargets",
        "objectRelationships",
        "objectChronicle",
        "jobs",
    }


def test_model_kind_mapping_is_complete_and_exact() -> None:
    assert modelByObjectTable == {
        "type": Type,
        "thing": Thing,
        "event": Event,
        "ember": Ember,
        "cog": Cog,
        "enigma": Enigma,
        "pondle": Pondle,
        "snug": Snug,
        "om": Om,
    }


@pytest.mark.parametrize("model", MODEL_CLASSES)
def test_concrete_model_has_expected_intel_columns(model: type[Base]) -> None:
    expected = {
        "id",
        "name",
        "locality",
        "stats",
        "statsUpdated",
        "knowledge",
        "knowledgeUpdated",
        "confidence",
        "confidenceUpdated",
    }
    assert expected <= set(model.__table__.columns.keys())


@pytest.mark.parametrize("model", [Type, Thing])
def test_wikipedia_models_have_wikipedia_ref(model: type[Base]) -> None:
    assert "wikipediaRef" in model.__table__.columns


@pytest.mark.parametrize("model", [Thing, Event, Ember, Cog, Enigma, Pondle, Snug, Om])
def test_every_primitive_has_one_required_type_reference(model: type[Base]) -> None:
    typeRef = model.__table__.c.typeRef

    assert typeRef.nullable is False
    assert str(typeRef.type.compile(dialect=postgresql.dialect())) == '"typeRef"'
    assert f"{model.__tablename__}TypeRefIdx" in {
        index.name for index in model.__table__.indexes
    }


@pytest.mark.parametrize("model", [Event, Ember, Cog, Enigma, Pondle, Snug, Om])
def test_non_wikipedia_models_do_not_gain_wikipedia_ref(model: type[Base]) -> None:
    assert "wikipediaRef" not in model.__table__.columns


@pytest.mark.parametrize("model", MODEL_CLASSES)
def test_concrete_model_identity_is_sole_primary_key_and_name_is_required(
    model: type[Base],
) -> None:
    assert [column.name for column in model.__table__.primary_key.columns] == ["id"]
    assert model.__table__.c.id.nullable is False
    assert model.__table__.c.name.nullable is False


@pytest.mark.parametrize("model", MODEL_CLASSES)
def test_every_concrete_model_compiles_with_quoted_ref_domain(model: type[Base]) -> None:
    sql = str(CreateTable(model.__table__).compile(dialect=postgresql.dialect()))
    kind = next(kind for kind, mapped in modelByObjectTable.items() if mapped is model)
    assert f'"{kind}Ref"' in sql
    assert '"statsUpdated"' in sql
    assert '"knowledgeUpdated"' in sql
    assert '"confidenceUpdated"' in sql


def test_linking_table_primary_keys_match_schema() -> None:
    assert [column.name for column in EventTarget.__table__.primary_key] == [
        "eventRef",
        "targetRef",
    ]
    assert [column.name for column in TypeParent.__table__.primary_key] == [
        "childRef",
        "parentRef",
    ]
    assert [column.name for column in TypeProperty.__table__.primary_key] == [
        "type",
        "name",
    ]
    assert [column.name for column in ObjectRelationship.__table__.primary_key] == [
        "sourceRef",
        "targetRef",
        "relationshipType",
    ]


def test_type_property_model_matches_cardinality_contract() -> None:
    table = TypeProperty.__table__
    assert table.c.name.type.length == 1024
    assert table.c.propertyTypeRef.nullable is False
    assert table.c.min.nullable is False
    assert table.c.max.nullable is False
    assert str(table.c.min.server_default.arg) == "1"
    assert str(table.c.max.server_default.arg) == "1"
    assert {constraint.name for constraint in table.constraints} >= {
        "typePropertiesMinCheck",
        "typePropertiesRangeCheck",
    }
    assert "typePropertiesPropertyTypeRefIdx" in {
        index.name for index in table.indexes
    }


def test_json_defaults_match_database_contract() -> None:
    assert str(ObjectRelationship.__table__.c.properties.server_default.arg) == "'{}'::jsonb"
    assert str(ObjectChronicle.__table__.c.details.server_default.arg) == "'{}'::jsonb"


def test_chronicle_identity_and_enum_match_database_contract() -> None:
    entryId = ObjectChronicle.__table__.c.entryId
    assert entryId.identity is not None
    assert entryId.identity.always is True
    assert set(ChronicleKind) == {ChronicleKind.ledger, ChronicleKind.provenance}
    assert {value.value for value in ChronicleKind} == {"ledger", "provenance"}


def test_models_can_be_constructed_with_python_object_refs() -> None:
    thing = Thing(
        id=ObjectRef.local("thing", "example"),
        typeRef=ObjectRef.local("type", "thing"),
        name="Example",
        confidence=Decimal("0.5"),
        stats={"count": 1},
        statsUpdated=datetime.now(UTC),
    )
    assert thing.id == "syn://thing/example"
    assert thing.typeRef == "syn://type/thing"
    assert thing.name == "Example"
    assert thing.stats == {"count": 1}


def test_registry_model_uses_general_object_ref_domain() -> None:
    sql = str(CreateTable(ObjectRegistry.__table__).compile(dialect=postgresql.dialect()))
    assert '"objectRef"' in sql


def test_job_model_matches_scheduler_persistence_contract() -> None:
    assert [column.name for column in Job.__table__.primary_key] == ["id"]
    assert Job.__table__.c.arguments.nullable is False
    assert str(Job.__table__.c.arguments.server_default.arg) == "'{}'::jsonb"
    assert {value.value for value in JobStatus} == {
        "queued", "running", "succeeded", "failed", "canceled"
    }
    sql = str(CreateTable(Job.__table__).compile(dialect=postgresql.dialect()))
    assert '"jobStatus"' in sql
    assert "UUID" in sql
