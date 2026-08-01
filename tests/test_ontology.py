from __future__ import annotations

from dataclasses import replace

import pytest

from syn import (
    ONTOLOGY_VERSION,
    QUESTION_DEFINITION,
    TYPE_BY_SLUG,
    TYPE_DEFINITIONS,
    TypeDefinition,
    ontologySqlStatements,
    validateOntology,
)


def test_curated_ontology_is_large_and_versioned() -> None:
    assert ONTOLOGY_VERSION == 2
    assert len(TYPE_DEFINITIONS) >= 350
    assert len(TYPE_BY_SLUG) == len(TYPE_DEFINITIONS)


def test_question_keeps_the_original_definition() -> None:
    question = TYPE_BY_SLUG["question"]
    assert question.parents == ("proposition",)
    assert question.definition == QUESTION_DEFINITION
    assert QUESTION_DEFINITION == (
        "A proposition which can be answered, either yes or no, or with any other "
        "kind of response."
    )


def test_polyhierarchy_contains_representative_multiple_parent_types() -> None:
    assert set(TYPE_BY_SLUG["person"].parents) == {"human-organism", "agent"}
    assert set(TYPE_BY_SLUG["company"].parents) == {
        "organization",
        "legal-entity",
        "economic-agent",
    }
    assert set(TYPE_BY_SLUG["building"].parents) == {"artifact", "spatial-thing"}
    assert set(TYPE_BY_SLUG["dataset"].parents) == {"information-object", "collection"}
    assert set(TYPE_BY_SLUG["network-measurement"].parents) == {
        "measurement-result", "data-point"
    }


def test_every_type_is_reachable_from_thing() -> None:
    for definition in TYPE_DEFINITIONS:
        frontier = [definition.slug]
        visited: set[str] = set()
        while frontier:
            current = frontier.pop()
            if current in visited:
                continue
            visited.add(current)
            frontier.extend(TYPE_BY_SLUG[current].parents)
        assert "thing" in visited


def test_validation_rejects_duplicate_slugs() -> None:
    duplicate = TypeDefinition("thing", (), "test")
    with pytest.raises(ValueError, match="unique"):
        validateOntology((duplicate, duplicate))


def test_validation_rejects_unknown_parent() -> None:
    values = (
        TypeDefinition("thing", (), "test"),
        TypeDefinition("child", ("missing",), "test"),
    )
    with pytest.raises(ValueError, match="unknown parent"):
        validateOntology(values)


def test_validation_rejects_disconnected_type() -> None:
    values = (
        TypeDefinition("thing", (), "test"),
        TypeDefinition("child", (), "test"),
    )
    with pytest.raises(ValueError, match="not connected"):
        validateOntology(values)


def test_validation_rejects_self_parent() -> None:
    values = (
        TypeDefinition("thing", (), "test"),
        TypeDefinition("child", ("child",), "test"),
    )
    with pytest.raises(ValueError, match="own parent"):
        validateOntology(values)


def test_validation_rejects_longer_cycle() -> None:
    values = (
        TypeDefinition("thing", (), "test"),
        TypeDefinition("first", ("second",), "test"),
        TypeDefinition("second", ("first",), "test"),
    )
    with pytest.raises(ValueError, match="cycle"):
        validateOntology(values)


def test_type_definition_derives_camel_case_name_and_object_ref() -> None:
    definition = TypeDefinition("human-organism", ("organism",), "biology")
    assert definition.name == "humanOrganism"
    assert definition.objectRef == "syn://type/human-organism"


def test_seed_sql_is_idempotent_and_contains_every_type_and_edge() -> None:
    sql = "\n".join(ontologySqlStatements())
    assert sql.count("ON CONFLICT (id) DO UPDATE") >= 1
    assert 'ON CONFLICT ("childRef", "parentRef") DO NOTHING' in sql
    for definition in TYPE_DEFINITIONS:
        assert definition.objectRef in sql
    assert QUESTION_DEFINITION in sql


def test_question_definition_is_not_accidentally_replaced() -> None:
    changed = replace(TYPE_BY_SLUG["question"], definition="A yes-or-no proposition.")
    assert changed.definition != QUESTION_DEFINITION
