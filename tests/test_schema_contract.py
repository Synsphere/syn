from __future__ import annotations

import re
from pathlib import Path

import pytest

from syn import LOCAL_KINDS


SCHEMA_PATH = Path("database/objectReferenceSchema.sql")


@pytest.fixture(scope="module")
def schema() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8-sig")


def test_schema_is_transactional(schema: str) -> None:
    assert schema.lstrip().startswith("BEGIN;")
    assert schema.count("COMMIT;") == 1
    assert schema.index("COMMIT;") > schema.index('CREATE INDEX "omsStatsGinIdx"')


def test_schema_contains_no_uuid_identity(schema: str) -> None:
    assert re.search(r"\buuid\b", schema, flags=re.IGNORECASE) is None
    assert "gen_random_uuid" not in schema


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
def test_every_kind_has_local_domain_table_and_domain_check(kind: str, schema: str) -> None:
    plural = {"type": "types", "thing": "things"}.get(kind, f"{kind}s")
    assert f'CREATE DOMAIN "{kind}Ref" AS "objectRef"' in schema
    assert f"CHECK (VALUE::text ~ '^syn://{kind}/')" in schema
    assert f"CREATE TABLE {plural} (" in schema
    assert f'id            "{kind}Ref" PRIMARY KEY' in schema
    assert f"CHECK (id::text ~ '^syn://{kind}/')" in schema


def test_general_object_ref_allows_non_syn_and_validates_syn(schema: str) -> None:
    assert "VALUE !~ '^syn://'" in schema
    assert "VALUE IS NOT NULL" not in schema.split('CREATE DOMAIN "objectRef"', 1)[1].split(
        'CREATE DOMAIN "typeRef"', 1
    )[0]
    kinds = "|".join(["type", "thing", "event", "ember", "cog", "enigma", "pondle", "snug", "om"])
    assert f"^syn://({kinds})/" in schema
    assert "VALUE !~ '[[:space:][:cntrl:]]'" in schema
    assert "regexp_replace(VALUE, '%[0-9A-Fa-f]{2}', '', 'g') !~ '%'" in schema


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
def test_every_kind_has_registry_and_identity_triggers(kind: str, schema: str) -> None:
    plural = {"type": "types", "thing": "things"}.get(kind, f"{kind}s")
    assert f'CREATE TRIGGER "{plural}PreventIdChange"' in schema
    assert f'CREATE TRIGGER "{plural}MaintainRegistry"' in schema
    assert f'CREATE TRIGGER "{plural}PreventTruncate"' in schema
    assert f'EXECUTE FUNCTION "maintainObjectRegistry"(\'{kind}\')' in schema


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
def test_placeholder_materialization_covers_every_kind(kind: str, schema: str) -> None:
    plural = {"type": "types", "thing": "things"}.get(kind, f"{kind}s")
    assert f"WHEN '{kind}' THEN" in schema
    assert f"INSERT INTO {plural} (id, name)" in schema
    assert f'"candidateRef"::"{kind}Ref"' in schema


def test_placeholder_creation_is_idempotent_and_silent(schema: str) -> None:
    ensureBody = schema.split('CREATE FUNCTION "ensureLocalObject"', 1)[1].split(
        'CREATE FUNCTION "requireLocalObjectIfSyn"', 1
    )[0]
    assert ensureBody.count("ON CONFLICT (id) DO NOTHING") == len(LOCAL_KINDS)
    assert "INSERT INTO \"objectRegistry\"" not in ensureBody


def test_external_refs_are_not_materialized(schema: str) -> None:
    assert 'IF "candidateRef"::text !~ \'^syn://\' THEN\n        RETURN;' in schema


def test_missing_ref_triggers_call_materializer(schema: str) -> None:
    assert schema.count('PERFORM "ensureLocalObject"("suppliedRef")') == 2


def test_all_concrete_models_enforce_confidence_range(schema: str) -> None:
    assert schema.count("CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)") == len(
        LOCAL_KINDS
    )


def test_expected_linking_constraints_exist(schema: str) -> None:
    assert 'PRIMARY KEY ("objectRef", "typeRef")' in schema
    assert 'PRIMARY KEY ("eventRef", "targetRef")' in schema
    assert 'PRIMARY KEY ("sourceRef", "targetRef", "relationshipType")' in schema
    assert "position integer CHECK (position IS NULL OR position >= 0)" in schema
    assert 'UNIQUE ("objectRef", kind, "sequenceNumber")' in schema
    assert 'CHECK ("endedAt" IS NULL OR "endedAt" >= "begunAt")' in schema


def test_registry_is_protected_from_direct_writes_and_truncate(schema: str) -> None:
    assert 'CREATE TRIGGER "objectRegistryProtectRows"' in schema
    assert 'CREATE TRIGGER "objectRegistryPreventTruncate"' in schema
    assert "objectRegistry\" is maintained by model-table triggers" in schema


def test_delete_guard_checks_every_linking_table(schema: str) -> None:
    body = schema.split('CREATE FUNCTION "preventReferencedLocalDelete"', 1)[1].split(
        'CREATE FUNCTION "preventRegistryBypass"', 1
    )[0]
    for table in ["objectTypes", "eventTargets", "objectRelationships", "objectChronicle"]:
        assert f'FROM "{table}"' in body


def test_all_expected_indexes_exist(schema: str) -> None:
    expected = {
        "objectTypesTypeIdx",
        "eventTargetsTargetIdx",
        "objectRelationshipsTargetIdx",
        "objectChronicleTimelineIdx",
        *(f"{table}StatsGinIdx" for table in [
            "types", "things", "events", "embers", "cogs", "enigmas", "pondles", "snugs", "oms"
        ]),
    }
    for index in expected:
        assert f'CREATE INDEX "{index}"' in schema


def test_camel_case_identifiers_are_quoted(schema: str) -> None:
    for identifier in [
        "objectRef",
        "statsUpdated",
        "knowledgeUpdated",
        "confidenceUpdated",
        "wikipediaRef",
        "objectRegistry",
        "objectRelationships",
    ]:
        assert f'"{identifier}"' in schema
