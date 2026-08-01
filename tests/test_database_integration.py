from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from syn import InvalidTypeParentsError, LOCAL_KINDS, ObjectRef, Type
from syn import buildFeedImportPlan, importFeedPlan
from test_feeds import feedRow, writeBundle

from conftest import MODEL_TABLES


pytestmark = pytest.mark.database


def token(prefix: str) -> str:
    return f"test-{prefix}-{uuid4().hex}"


def localRef(kind: str, slug: str) -> str:
    return f"syn://{kind}/{slug}"


def expectDatabaseError(db: Connection, operation: Callable[[], object]) -> DBAPIError:
    savepoint = db.begin_nested()
    try:
        with pytest.raises(DBAPIError) as caught:
            operation()
        return caught.value
    finally:
        savepoint.rollback()


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/x",
        "mailto:user@example.com",
        "/relative/path",
        "#fragment",
        "syn://thing/example",
    ],
)
def test_object_ref_domain_accepts_supported_url_references(db: Connection, value: str) -> None:
    assert db.scalar(text('SELECT CAST(:value AS "objectRef")'), {"value": value}) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "has space",
        "line\nbreak",
        "%",
        "%GG",
        "syn://unknown/x",
        "syn://thing/Upper",
        "syn://thing/-leading",
        "syn://thing/trailing-",
        "syn://thing/a--b",
    ],
)
def test_object_ref_domain_rejects_invalid_values(db: Connection, value: str) -> None:
    expectDatabaseError(
        db, lambda: db.execute(text('SELECT CAST(:value AS "objectRef")'), {"value": value})
    )


def test_object_ref_domain_allows_null_for_nullable_columns(db: Connection) -> None:
    assert db.scalar(text('SELECT CAST(NULL AS "objectRef")')) is None


@pytest.mark.parametrize(
    "value",
    ["a", "abc", "a-b", "a_b", "a1-b2_c3", "x" * 255],
)
def test_slug_domain_accepts_valid_values(db: Connection, value: str) -> None:
    assert db.scalar(text("SELECT CAST(:value AS slug)"), {"value": value}) == value


@pytest.mark.parametrize(
    "value",
    ["", "Upper", "-leading", "trailing-", "_leading", "trailing_", "a--b", "a__b", "a b", "x" * 256],
)
def test_slug_domain_rejects_invalid_values(db: Connection, value: str) -> None:
    expectDatabaseError(
        db, lambda: db.execute(text("SELECT CAST(:value AS slug)"), {"value": value})
    )


def test_object_ref_helper_functions_for_local_ref(db: Connection) -> None:
    reference = localRef("thing", "helper-example")
    row = db.execute(
        text(
            'SELECT "objectRefTable"(CAST(:ref AS "objectRef")) AS table_name, '
            '"objectRefSlug"(CAST(:ref AS "objectRef")) AS slug'
        ),
        {"ref": reference},
    ).one()
    assert row.table_name == "thing"
    assert row.slug == "helper-example"


def test_object_ref_helper_functions_return_null_for_external_ref(db: Connection) -> None:
    row = db.execute(
        text(
            'SELECT "objectRefTable"(CAST(:ref AS "objectRef")) AS table_name, '
            '"objectRefSlug"(CAST(:ref AS "objectRef")) AS slug'
        ),
        {"ref": "https://example.com/x"},
    ).one()
    assert row.table_name is None
    assert row.slug is None


def test_object_ref_helper_functions_are_strict_for_null(db: Connection) -> None:
    row = db.execute(
        text(
            'SELECT "objectRefTable"(NULL::"objectRef") AS table_name, '
            '"objectRefSlug"(NULL::"objectRef") AS slug'
        )
    ).one()
    assert row.table_name is None
    assert row.slug is None


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Crème brûlée", "creme-brulee"),
        ("__double__", "double"),
        ("mixed-_separator", "mixed-separator"),
        ("Hello, World!", "hello-world"),
    ],
)
def test_database_slug_normalizer(source: str, expected: str, db: Connection) -> None:
    assert db.scalar(text('SELECT "normalizeSlug"(:value)'), {"value": source}) == expected


def test_make_object_ref_builds_canonical_local_ref(db: Connection) -> None:
    assert db.scalar(
        text('SELECT "makeObjectRef"(\'thing\', CAST(:slug AS slug))::text'),
        {"slug": "made-ref"},
    ) == "syn://thing/made-ref"


def test_make_object_ref_rejects_unknown_local_kind(db: Connection) -> None:
    expectDatabaseError(
        db,
        lambda: db.execute(
            text('SELECT "makeObjectRef"(\'unknown\', CAST(:slug AS slug))'),
            {"slug": "invalid-kind"},
        ),
    )


@pytest.mark.parametrize(
    ("domain", "valid", "invalid"),
    [
        ("observationRef", "syn://event/observation", "syn://thing/observation"),
        ("balanceRef", "syn://ember/balance", "syn://thing/balance"),
    ],
)
def test_specialized_reference_domains(
    db: Connection, domain: str, valid: str, invalid: str
) -> None:
    assert db.scalar(text(f'SELECT CAST(:value AS "{domain}")'), {"value": valid}) == valid
    expectDatabaseError(
        db,
        lambda: db.execute(text(f'SELECT CAST(:value AS "{domain}")'), {"value": invalid}),
    )


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
def test_typed_ref_domains_accept_exact_kind(db: Connection, kind: str) -> None:
    value = localRef(kind, token("typed"))
    assert db.scalar(text(f'SELECT CAST(:value AS "{kind}Ref")'), {"value": value}) == value


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
def test_typed_ref_domains_reject_external_values(db: Connection, kind: str) -> None:
    expectDatabaseError(
        db,
        lambda: db.execute(
            text(f'SELECT CAST(:value AS "{kind}Ref")'), {"value": "https://example.com/x"}
        ),
    )


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
def test_typed_ref_domains_reject_wrong_local_kind(db: Connection, kind: str) -> None:
    wrongKind = "thing" if kind != "thing" else "event"
    expectDatabaseError(
        db,
        lambda: db.execute(
            text(f'SELECT CAST(:value AS "{kind}Ref")'),
            {"value": localRef(wrongKind, token("wrong-kind"))},
        ),
    )


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
def test_insert_registers_every_concrete_object_kind(db: Connection, kind: str) -> None:
    slug = token("register")
    reference = localRef(kind, slug)
    table = MODEL_TABLES[kind]
    typeColumn = ', "typeRef"' if kind == "thing" else ""
    typeValue = ", 'syn://type/thing'" if kind == "thing" else ""
    db.execute(
        text(
            f'INSERT INTO {table} (id, name{typeColumn}) '
            f'VALUES (CAST(:id AS "{kind}Ref"), :name{typeValue})'
        ),
        {"id": reference, "name": "Registered"},
    )
    assert db.scalar(text('SELECT count(*) FROM "objectRegistry" WHERE id = :id'), {"id": reference}) == 1


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
def test_delete_removes_unreferenced_registry_entry(db: Connection, kind: str) -> None:
    reference = localRef(kind, token("delete"))
    table = MODEL_TABLES[kind]
    typeColumn = ', "typeRef"' if kind == "thing" else ""
    typeValue = ", 'syn://type/thing'" if kind == "thing" else ""
    db.execute(
        text(
            f'INSERT INTO {table} (id, name{typeColumn}) '
            f'VALUES (CAST(:id AS "{kind}Ref"), :name{typeValue})'
        ),
        {"id": reference, "name": "Delete me"},
    )
    db.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": reference})
    assert db.scalar(text('SELECT count(*) FROM "objectRegistry" WHERE id = :id'), {"id": reference}) == 0


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
def test_concrete_identity_is_immutable(db: Connection, kind: str) -> None:
    reference = localRef(kind, token("immutable"))
    replacement = localRef(kind, token("replacement"))
    table = MODEL_TABLES[kind]
    typeColumn = ', "typeRef"' if kind == "thing" else ""
    typeValue = ", 'syn://type/thing'" if kind == "thing" else ""
    db.execute(
        text(
            f'INSERT INTO {table} (id, name{typeColumn}) '
            f'VALUES (CAST(:id AS "{kind}Ref"), :name{typeValue})'
        ),
        {"id": reference, "name": "Immutable"},
    )
    error = expectDatabaseError(
        db,
        lambda: db.execute(
            text(f'UPDATE {table} SET id = CAST(:replacement AS "{kind}Ref") WHERE id = :id'),
            {"replacement": replacement, "id": reference},
        ),
    )
    assert "immutable" in str(error).lower()


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
def test_concrete_name_is_required(db: Connection, kind: str) -> None:
    table = MODEL_TABLES[kind]
    typeColumn = ', "typeRef"' if kind == "thing" else ""
    typeValue = ", 'syn://type/thing'" if kind == "thing" else ""
    expectDatabaseError(
        db,
        lambda: db.execute(
            text(
                f'INSERT INTO {table} (id, name{typeColumn}) '
                f'VALUES (CAST(:id AS "{kind}Ref"), NULL{typeValue})'
            ),
            {"id": localRef(kind, token("null-name"))},
        ),
    )


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
@pytest.mark.parametrize("confidence", [0, 1, 0.5])
def test_confidence_accepts_closed_unit_interval(
    db: Connection, kind: str, confidence: float
) -> None:
    table = MODEL_TABLES[kind]
    typeColumn = ', "typeRef"' if kind == "thing" else ""
    typeValue = ", 'syn://type/thing'" if kind == "thing" else ""
    db.execute(
        text(
            f'INSERT INTO {table} (id, name, confidence{typeColumn}) '
            f'VALUES (CAST(:id AS "{kind}Ref"), :name, :confidence{typeValue})'
        ),
        {"id": localRef(kind, token("confidence-valid")), "name": "Valid", "confidence": confidence},
    )


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
@pytest.mark.parametrize("confidence", [-0.0001, 1.0001, -1, 2])
def test_confidence_rejects_values_outside_unit_interval(
    db: Connection, kind: str, confidence: float
) -> None:
    table = MODEL_TABLES[kind]
    typeColumn = ', "typeRef"' if kind == "thing" else ""
    typeValue = ", 'syn://type/thing'" if kind == "thing" else ""
    expectDatabaseError(
        db,
        lambda: db.execute(
            text(
                f'INSERT INTO {table} (id, name, confidence{typeColumn}) '
                f'VALUES (CAST(:id AS "{kind}Ref"), :name, :confidence{typeValue})'
            ),
            {
                "id": localRef(kind, token("confidence-invalid")),
                "name": "Invalid",
                "confidence": confidence,
            },
        ),
    )


@pytest.mark.parametrize("special", ["NaN", "Infinity", "-Infinity"])
def test_confidence_rejects_non_finite_numeric_values(db: Connection, special: str) -> None:
    expectDatabaseError(
        db,
        lambda: db.execute(
            text(
                'INSERT INTO things (id, "typeRef", name, confidence) '
                "VALUES (CAST(:id AS \"thingRef\"), 'syn://type/thing', "
                ':name, CAST(:confidence AS numeric))'
            ),
            {
                "id": localRef("thing", token("non-finite-confidence")),
                "name": "Invalid",
                "confidence": special,
            },
        ),
    )


def test_jsonb_intel_fields_round_trip(db: Connection) -> None:
    reference = localRef("thing", token("jsonb"))
    row = db.execute(
        text(
            'INSERT INTO things (id, "typeRef", name, stats, knowledge) '
            "VALUES (CAST(:id AS \"thingRef\"), 'syn://type/thing', "
            ':name, CAST(:stats AS jsonb), '
            'CAST(:knowledge AS jsonb)) RETURNING stats, knowledge'
        ),
        {
            "id": reference,
            "name": "JSON",
            "stats": '{"count": 2, "nested": {"ok": true}}',
            "knowledge": '{"description": "known"}',
        },
    ).one()
    assert row.stats == {"count": 2, "nested": {"ok": True}}
    assert row.knowledge == {"description": "known"}


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
def test_duplicate_concrete_identity_is_rejected(db: Connection, kind: str) -> None:
    reference = localRef(kind, token("duplicate"))
    table = MODEL_TABLES[kind]
    typeColumn = ', "typeRef"' if kind == "thing" else ""
    typeValue = ", 'syn://type/thing'" if kind == "thing" else ""
    statement = text(
        f'INSERT INTO {table} (id, name{typeColumn}) '
        f'VALUES (CAST(:id AS "{kind}Ref"), :name{typeValue})'
    )
    db.execute(statement, {"id": reference, "name": "First"})
    expectDatabaseError(db, lambda: db.execute(statement, {"id": reference, "name": "Second"}))


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
def test_generic_relationship_materializes_missing_local_placeholders(
    db: Connection, kind: str
) -> None:
    slug = token("placeholder")
    reference = localRef(kind, slug)
    external = f"https://example.com/{uuid4().hex}"
    db.execute(
        text(
            'INSERT INTO "objectRelationships" '
            '("sourceRef", "targetRef", "relationshipType") '
            "VALUES (:source, :target, 'references')"
        ),
        {"source": reference, "target": external},
    )
    table = MODEL_TABLES[kind]
    row = db.execute(text(f"SELECT id, name, stats, knowledge, confidence FROM {table} WHERE id = :id"), {"id": reference}).one()
    assert row.id == reference
    assert row.name == slug
    assert row.stats is None
    assert row.knowledge is None
    assert row.confidence is None
    assert db.scalar(text('SELECT count(*) FROM "objectRegistry" WHERE id = :id'), {"id": reference}) == 1


def test_external_relationship_refs_do_not_materialize_registry_rows(db: Connection) -> None:
    source = f"https://source.example/{uuid4().hex}"
    target = f"https://target.example/{uuid4().hex}"
    db.execute(
        text(
            'INSERT INTO "objectRelationships" '
            '("sourceRef", "targetRef", "relationshipType") '
            "VALUES (:source, :target, 'external')"
        ),
        {"source": source, "target": target},
    )
    assert db.scalar(
        text('SELECT count(*) FROM "objectRegistry" WHERE id IN (:source, :target)'),
        {"source": source, "target": target},
    ) == 0


def test_direct_materializer_ignores_external_ref(db: Connection) -> None:
    reference = f"https://example.com/{uuid4().hex}"
    db.execute(text('SELECT "ensureLocalObject"(CAST(:ref AS "objectRef"))'), {"ref": reference})
    assert db.scalar(text('SELECT count(*) FROM "objectRegistry" WHERE id = :id'), {"id": reference}) == 0


@pytest.mark.parametrize("kind", sorted(LOCAL_KINDS))
def test_direct_materializer_is_idempotent(db: Connection, kind: str) -> None:
    slug = token("direct-materialize")
    reference = localRef(kind, slug)
    statement = text('SELECT "ensureLocalObject"(CAST(:ref AS "objectRef"))')
    db.execute(statement, {"ref": reference})
    db.execute(statement, {"ref": reference})
    table = MODEL_TABLES[kind]
    assert db.scalar(text(f"SELECT count(*) FROM {table} WHERE id = :id"), {"id": reference}) == 1
    assert db.scalar(text('SELECT count(*) FROM "objectRegistry" WHERE id = :id'), {"id": reference}) == 1


def test_materialization_never_overwrites_existing_object(db: Connection) -> None:
    reference = localRef("thing", token("preserve"))
    db.execute(
        text(
            'INSERT INTO things (id, "typeRef", name) '
            "VALUES (CAST(:id AS \"thingRef\"), 'syn://type/thing', :name)"
        ),
        {"id": reference, "name": "Real name"},
    )
    db.execute(
        text(
            'INSERT INTO "objectRelationships" '
            '("sourceRef", "targetRef", "relationshipType") '
            "VALUES (:source, 'https://example.com/target', 'preserves')"
        ),
        {"source": reference},
    )
    assert db.scalar(text("SELECT name FROM things WHERE id = :id"), {"id": reference}) == "Real name"


def test_object_types_materializes_object_and_type(db: Connection) -> None:
    objectRef = localRef("cog", token("classified"))
    typeRef = localRef("type", token("classification"))
    db.execute(
        text('INSERT INTO "objectTypes" ("objectRef", "typeRef") VALUES (:object, :type)'),
        {"object": objectRef, "type": typeRef},
    )
    assert db.scalar(text("SELECT count(*) FROM cogs WHERE id = :id"), {"id": objectRef}) == 1
    assert db.scalar(text("SELECT count(*) FROM types WHERE id = :id"), {"id": typeRef}) == 1


def test_thing_requires_exactly_one_type_reference(db: Connection) -> None:
    expectDatabaseError(
        db,
        lambda: db.execute(
            text('INSERT INTO things (id, name) VALUES (CAST(:id AS "thingRef"), :name)'),
            {"id": localRef("thing", token("missing-type")), "name": "Missing type"},
        ),
    )


def test_thing_type_reference_materializes_a_missing_type(db: Connection) -> None:
    thingRef = localRef("thing", token("typed-thing"))
    typeRef = localRef("type", token("materialized-thing-type"))

    db.execute(
        text(
            'INSERT INTO things (id, "typeRef", name) '
            'VALUES (CAST(:thing AS "thingRef"), CAST(:type AS "typeRef"), :name)'
        ),
        {"thing": thingRef, "type": typeRef, "name": "Typed thing"},
    )

    assert db.scalar(text('SELECT "typeRef"::text FROM things WHERE id = :id'), {"id": thingRef}) == typeRef
    assert db.scalar(text("SELECT count(*) FROM types WHERE id = :id"), {"id": typeRef}) == 1


def test_object_types_rejects_local_things(db: Connection) -> None:
    thingRef = localRef("thing", token("legacy-object-type"))
    db.execute(
        text(
            'INSERT INTO things (id, "typeRef", name) '
            "VALUES (CAST(:thing AS \"thingRef\"), 'syn://type/thing', :name)"
        ),
        {"thing": thingRef, "name": "Single typed thing"},
    )

    error = expectDatabaseError(
        db,
        lambda: db.execute(
            text(
                'INSERT INTO "objectTypes" ("objectRef", "typeRef") '
                "VALUES (:thing, 'syn://type/thing')"
            ),
            {"thing": thingRef},
        ),
    )
    assert "objectTypesNoThingCheck" in str(error)


def test_duplicate_object_type_pair_is_rejected(db: Connection) -> None:
    values = {
        "object": localRef("cog", token("typed-object")),
        "type": localRef("type", token("typed-type")),
    }
    statement = text('INSERT INTO "objectTypes" ("objectRef", "typeRef") VALUES (:object, :type)')
    db.execute(statement, values)
    expectDatabaseError(db, lambda: db.execute(statement, values))


def test_event_targets_materializes_event_and_target(db: Connection) -> None:
    eventRef = localRef("event", token("event"))
    targetRef = localRef("pondle", token("target"))
    db.execute(
        text(
            'INSERT INTO "eventTargets" ("eventRef", "targetRef", role, position) '
            "VALUES (:event, :target, 'subject', 0)"
        ),
        {"event": eventRef, "target": targetRef},
    )
    assert db.scalar(text("SELECT count(*) FROM events WHERE id = :id"), {"id": eventRef}) == 1
    assert db.scalar(text("SELECT count(*) FROM pondles WHERE id = :id"), {"id": targetRef}) == 1


def test_event_target_rejects_negative_position(db: Connection) -> None:
    expectDatabaseError(
        db,
        lambda: db.execute(
            text(
                'INSERT INTO "eventTargets" ("eventRef", "targetRef", position) '
                "VALUES (:event, :target, -1)"
            ),
            {
                "event": localRef("event", token("negative-position")),
                "target": "https://example.com/target",
            },
        ),
    )


def test_event_target_rejects_integer_overflow(db: Connection) -> None:
    expectDatabaseError(
        db,
        lambda: db.execute(
            text(
                'INSERT INTO "eventTargets" ("eventRef", "targetRef", position) '
                "VALUES (:event, :target, 2147483648)"
            ),
            {
                "event": localRef("event", token("overflow-position")),
                "target": "https://example.com/target",
            },
        ),
    )


@pytest.mark.parametrize("column", ["eventRef", "targetRef"])
def test_event_target_primary_key_columns_are_required(db: Connection, column: str) -> None:
    values = {
        "event": None if column == "eventRef" else localRef("event", token("required-event")),
        "target": None if column == "targetRef" else "https://example.com/target",
    }
    expectDatabaseError(
        db,
        lambda: db.execute(
            text(
                'INSERT INTO "eventTargets" ("eventRef", "targetRef") '
                "VALUES (:event, :target)"
            ),
            values,
        ),
    )


@pytest.mark.parametrize("role", ["Upper", "two words", "-leading", "a--b"])
def test_event_target_role_must_be_valid_slug(db: Connection, role: str) -> None:
    expectDatabaseError(
        db,
        lambda: db.execute(
            text(
                'INSERT INTO "eventTargets" ("eventRef", "targetRef", role) '
                "VALUES (:event, :target, :role)"
            ),
            {
                "event": localRef("event", token("bad-role")),
                "target": "https://example.com/target",
                "role": role,
            },
        ),
    )


@pytest.mark.parametrize("position", [None, 0, 1, 2_147_483_647])
def test_event_target_accepts_valid_positions(db: Connection, position: int | None) -> None:
    db.execute(
        text(
            'INSERT INTO "eventTargets" ("eventRef", "targetRef", position) '
            "VALUES (:event, :target, :position)"
        ),
        {
            "event": localRef("event", token("valid-position")),
            "target": f"https://example.com/{uuid4().hex}",
            "position": position,
        },
    )


def test_relationship_properties_default_to_empty_json(db: Connection) -> None:
    source = f"https://source.example/{uuid4().hex}"
    target = f"https://target.example/{uuid4().hex}"
    properties = db.scalar(
        text(
            'INSERT INTO "objectRelationships" '
            '("sourceRef", "targetRef", "relationshipType") '
            "VALUES (:source, :target, 'default-json') RETURNING properties"
        ),
        {"source": source, "target": target},
    )
    assert properties == {}


@pytest.mark.parametrize("relationshipType", ["Upper", "two words", "-leading", "a--b"])
def test_relationship_type_must_be_valid_slug(db: Connection, relationshipType: str) -> None:
    expectDatabaseError(
        db,
        lambda: db.execute(
            text(
                'INSERT INTO "objectRelationships" '
                '("sourceRef", "targetRef", "relationshipType") '
                "VALUES (:source, :target, :relationshipType)"
            ),
            {
                "source": "https://example.com/source",
                "target": "https://example.com/target",
                "relationshipType": relationshipType,
            },
        ),
    )


@pytest.mark.parametrize("column", ["sourceRef", "targetRef", "relationshipType"])
def test_relationship_primary_key_columns_are_required(db: Connection, column: str) -> None:
    values = {
        "source": None if column == "sourceRef" else "https://example.com/source",
        "target": None if column == "targetRef" else "https://example.com/target",
        "kind": None if column == "relationshipType" else "required",
    }
    expectDatabaseError(
        db,
        lambda: db.execute(
            text(
                'INSERT INTO "objectRelationships" '
                '("sourceRef", "targetRef", "relationshipType") '
                "VALUES (:source, :target, :kind)"
            ),
            values,
        ),
    )


def test_chronicle_materializes_object_and_event_and_defaults_details(db: Connection) -> None:
    objectRef = localRef("snug", token("chronicle-object"))
    eventRef = localRef("event", token("chronicle-event"))
    details = db.scalar(
        text(
            'INSERT INTO "objectChronicle" '
            '("objectRef", "eventRef", kind, "begunAt", "sequenceNumber") '
            "VALUES (:object, :event, 'ledger', :begun, 1) RETURNING details"
        ),
        {"object": objectRef, "event": eventRef, "begun": datetime.now(UTC)},
    )
    assert details == {}
    assert db.scalar(text("SELECT count(*) FROM snugs WHERE id = :id"), {"id": objectRef}) == 1
    assert db.scalar(text("SELECT count(*) FROM events WHERE id = :id"), {"id": eventRef}) == 1


def test_chronicle_allows_null_event(db: Connection) -> None:
    db.execute(
        text(
            'INSERT INTO "objectChronicle" '
            '("objectRef", "eventRef", kind, "begunAt", "sequenceNumber") '
            "VALUES (:object, NULL, 'provenance', :begun, 1)"
        ),
        {"object": localRef("om", token("no-event")), "begun": datetime.now(UTC)},
    )


def test_chronicle_rejects_end_before_begin(db: Connection) -> None:
    begun = datetime.now(UTC)
    expectDatabaseError(
        db,
        lambda: db.execute(
            text(
                'INSERT INTO "objectChronicle" '
                '("objectRef", kind, "begunAt", "endedAt", "sequenceNumber") '
                "VALUES (:object, 'ledger', :begun, :ended, 1)"
            ),
            {
                "object": localRef("ember", token("bad-time")),
                "begun": begun,
                "ended": begun - timedelta(seconds=1),
            },
        ),
    )


def test_chronicle_accepts_equal_or_later_end(db: Connection) -> None:
    begun = datetime.now(UTC)
    for sequence, ended in [(1, begun), (2, begun + timedelta(seconds=1)), (3, None)]:
        db.execute(
            text(
                'INSERT INTO "objectChronicle" '
                '("objectRef", kind, "begunAt", "endedAt", "sequenceNumber") '
                "VALUES (:object, 'ledger', :begun, :ended, :sequence)"
            ),
            {
                "object": localRef("ember", token(f"good-time-{sequence}")),
                "begun": begun,
                "ended": ended,
                "sequence": sequence,
            },
        )


def test_chronicle_rejects_duplicate_object_kind_sequence(db: Connection) -> None:
    values = {
        "object": localRef("thing", token("duplicate-sequence")),
        "begun": datetime.now(UTC),
    }
    statement = text(
        'INSERT INTO "objectChronicle" ("objectRef", kind, "begunAt", "sequenceNumber") '
        "VALUES (:object, 'ledger', :begun, 1)"
    )
    db.execute(statement, values)
    expectDatabaseError(db, lambda: db.execute(statement, values))


def test_chronicle_rejects_unknown_kind(db: Connection) -> None:
    expectDatabaseError(
        db,
        lambda: db.execute(
            text(
                'INSERT INTO "objectChronicle" '
                '("objectRef", kind, "begunAt", "sequenceNumber") '
                "VALUES (:object, 'unknown', :begun, 1)"
            ),
            {"object": localRef("thing", token("bad-kind")), "begun": datetime.now(UTC)},
        ),
    )


@pytest.mark.parametrize("column", ["objectRef", "kind", "begunAt", "sequenceNumber"])
def test_chronicle_required_columns_reject_null(db: Connection, column: str) -> None:
    values = {
        "object": None if column == "objectRef" else localRef("thing", token("required-chronicle")),
        "kind": None if column == "kind" else "ledger",
        "begun": None if column == "begunAt" else datetime.now(UTC),
        "sequence": None if column == "sequenceNumber" else 1,
    }
    expectDatabaseError(
        db,
        lambda: db.execute(
            text(
                'INSERT INTO "objectChronicle" '
                '("objectRef", kind, "begunAt", "sequenceNumber") '
                "VALUES (:object, :kind, :begun, :sequence)"
            ),
            values,
        ),
    )


def test_chronicle_allows_negative_sequence_number_as_currently_designed(db: Connection) -> None:
    db.execute(
        text(
            'INSERT INTO "objectChronicle" '
            '("objectRef", kind, "begunAt", "sequenceNumber") '
            "VALUES (:object, 'ledger', :begun, -1)"
        ),
        {"object": localRef("thing", token("negative-sequence")), "begun": datetime.now(UTC)},
    )


def test_direct_registry_insert_is_rejected(db: Connection) -> None:
    error = expectDatabaseError(
        db,
        lambda: db.execute(
            text('INSERT INTO "objectRegistry" (id) VALUES (:id)'),
            {"id": localRef("thing", token("direct-registry"))},
        ),
    )
    assert "maintained by model-table triggers" in str(error)


def test_direct_registry_update_is_rejected(db: Connection) -> None:
    reference = localRef("thing", token("registry-update"))
    db.execute(
        text(
            'INSERT INTO things (id, "typeRef", name) '
            "VALUES (CAST(:id AS \"thingRef\"), 'syn://type/thing', :name)"
        ),
        {"id": reference, "name": "Registry"},
    )
    expectDatabaseError(
        db,
        lambda: db.execute(
            text('UPDATE "objectRegistry" SET id = :new WHERE id = :old'),
            {"new": localRef("thing", token("changed")), "old": reference},
        ),
    )


def test_direct_registry_delete_is_rejected(db: Connection) -> None:
    reference = localRef("thing", token("registry-delete"))
    db.execute(
        text(
            'INSERT INTO things (id, "typeRef", name) '
            "VALUES (CAST(:id AS \"thingRef\"), 'syn://type/thing', :name)"
        ),
        {"id": reference, "name": "Registry"},
    )
    expectDatabaseError(
        db, lambda: db.execute(text('DELETE FROM "objectRegistry" WHERE id = :id'), {"id": reference})
    )


@pytest.mark.parametrize("table", ["objectRegistry", *MODEL_TABLES.values()])
def test_truncate_is_rejected(db: Connection, table: str) -> None:
    quoted = f'"{table}"' if any(character.isupper() for character in table) else table
    error = expectDatabaseError(db, lambda: db.execute(text(f"TRUNCATE TABLE {quoted}")))
    assert "TRUNCATE is disabled" in str(error)


def test_deleting_referenced_local_object_is_rejected(db: Connection) -> None:
    reference = localRef("thing", token("referenced-delete"))
    db.execute(
        text(
            'INSERT INTO "objectRelationships" '
            '("sourceRef", "targetRef", "relationshipType") '
            "VALUES (:source, 'https://example.com/target', 'blocks-delete')"
        ),
        {"source": reference},
    )
    error = expectDatabaseError(
        db, lambda: db.execute(text("DELETE FROM things WHERE id = :id"), {"id": reference})
    )
    assert "cannot delete referenced local ObjectRef" in str(error)


@pytest.mark.parametrize("linkTable", ["objectTypes", "eventTargets", "objectChronicle"])
def test_delete_guard_covers_other_link_tables(db: Connection, linkTable: str) -> None:
    kind = "cog" if linkTable == "objectTypes" else "thing"
    reference = localRef(kind, token(f"delete-{linkTable.lower()}"))
    if linkTable == "objectTypes":
        db.execute(
            text('INSERT INTO "objectTypes" ("objectRef", "typeRef") VALUES (:object, :type)'),
            {"object": reference, "type": localRef("type", token("delete-type"))},
        )
    elif linkTable == "eventTargets":
        db.execute(
            text('INSERT INTO "eventTargets" ("eventRef", "targetRef") VALUES (:event, :target)'),
            {"event": localRef("event", token("delete-event")), "target": reference},
        )
    else:
        db.execute(
            text(
                'INSERT INTO "objectChronicle" '
                '("objectRef", kind, "begunAt", "sequenceNumber") '
                "VALUES (:object, 'ledger', :begun, 1)"
            ),
            {"object": reference, "begun": datetime.now(UTC)},
        )

    expectDatabaseError(
        db,
        lambda: db.execute(
            text(f"DELETE FROM {MODEL_TABLES[kind]} WHERE id = :id"),
            {"id": reference},
        ),
    )


def test_assert_object_has_type_succeeds_for_classified_object(db: Connection) -> None:
    objectRef = localRef("thing", token("assert-classified"))
    typeSlug = token("assert-type")
    typeRef = localRef("type", typeSlug)
    db.execute(
        text(
            'INSERT INTO things (id, "typeRef", name) '
            'VALUES (CAST(:object AS "thingRef"), CAST(:type AS "typeRef"), :name)'
        ),
        {"object": objectRef, "type": typeRef, "name": "Classified"},
    )
    db.execute(
        text('SELECT "assertObjectHasType"(CAST(:object AS "objectRef"), CAST(:slug AS slug))'),
        {"object": objectRef, "slug": typeSlug},
    )


def test_assert_object_has_type_rejects_unclassified_object(db: Connection) -> None:
    objectRef = localRef("thing", token("assert-unclassified"))
    typeSlug = token("missing-type")
    expectDatabaseError(
        db,
        lambda: db.execute(
            text('SELECT "assertObjectHasType"(CAST(:object AS "objectRef"), CAST(:slug AS slug))'),
            {"object": objectRef, "slug": typeSlug},
        ),
    )


def test_all_expected_indexes_are_installed(db: Connection) -> None:
    expected = {
        "objectTypesTypeIdx",
        "thingsTypeRefIdx",
        "typeParentsParentIdx",
        "eventTargetsTargetIdx",
        "objectRelationshipsTargetIdx",
        "objectChronicleTimelineIdx",
        *(f"{table}StatsGinIdx" for table in MODEL_TABLES.values()),
    }
    installed = set(
        db.scalars(
            text("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
        )
    )
    assert expected <= installed


def test_curated_type_ontology_is_seeded(db: Connection) -> None:
    assert db.scalar(text("SELECT count(*) FROM types")) >= 350
    assert db.scalar(text('SELECT count(*) FROM "typeParents"')) >= 400


def test_every_installed_thing_has_one_direct_type_and_no_legacy_link(db: Connection) -> None:
    assert db.scalar(text('SELECT count(*) FROM things WHERE "typeRef" IS NULL')) == 0
    assert db.scalar(
        text(
            'SELECT count(*) FROM "objectTypes" '
            "WHERE \"objectRef\"::text ~ '^syn://thing/'"
        )
    ) == 0


def test_type_intersection_resolver_is_idempotent_and_valid(db: Connection) -> None:
    left = localRef("type", token("intersection-left"))
    right = localRef("type", token("intersection-right"))
    for reference, name in ((left, "intersectionLeft"), (right, "intersectionRight")):
        db.execute(
            text("INSERT INTO types (id, name) VALUES (CAST(:id AS \"typeRef\"), :name)"),
            {"id": reference, "name": name},
        )
        db.execute(
            text(
                'INSERT INTO "typeParents" ("childRef", "parentRef") '
                "VALUES (CAST(:id AS \"typeRef\"), 'syn://type/thing')"
            ),
            {"id": reference},
        )

    parameters = {"parents": [left, right]}
    statement = text(
        'SELECT "resolveTypeIntersection"(CAST(:parents AS "typeRef"[]))::text'
    )
    first = db.scalar(statement, parameters)
    second = db.scalar(statement, parameters)

    assert first == second
    assert set(
        db.scalars(
            text(
                'SELECT "parentRef"::text FROM "typeParents" '
                'WHERE "childRef"::text = :child'
            ),
            {"child": first},
        )
    ) == {left, right}
    assert db.scalar(
        text("SELECT knowledge ->> 'generationKind' FROM types WHERE id = :id"),
        {"id": first},
    ) == "typeIntersection"


def test_type_intersection_resolver_removes_redundant_ancestors(db: Connection) -> None:
    result = db.scalar(
        text(
            'SELECT "resolveTypeIntersection"('
            "ARRAY['syn://type/company', 'syn://type/organization', "
            "'syn://type/legal-entity']::\"typeRef\"[])::text"
        )
    )

    assert result == "syn://type/company"


def test_type_model_rejects_transitively_redundant_parents(db: Connection) -> None:
    with Session(bind=db, join_transaction_mode="create_savepoint") as session:
        person = session.get(Type, ObjectRef("syn://type/person"))
        assert person is not None

        with pytest.raises(
            InvalidTypeParentsError,
            match=(
                "syn://type/organism is an ancestor of "
                "syn://type/human-organism"
            ),
        ):
            person.replaceParents(
                session,
                [
                    ObjectRef("syn://type/human-organism"),
                    ObjectRef("syn://type/agent"),
                    ObjectRef("syn://type/organism"),
                ],
            )


def test_feed_type_ontology_is_seeded(db: Connection) -> None:
    assert db.scalar(
        text("SELECT count(*) FROM types WHERE knowledge ->> 'ontology' = 'syn-feeds'")
    ) >= 75
    parents = set(
        db.scalars(
            text(
                'SELECT "parentRef"::text FROM "typeParents" '
                "WHERE \"childRef\" = 'syn://type/podcast-feed'"
            )
        )
    )
    assert parents == {"syn://type/rss-feed", "syn://type/media-feed"}


def test_feed_import_upserts_thing_knowledge_and_inherited_types(
    db: Connection, tmp_path
) -> None:
    bundle = writeBundle(tmp_path / "feeds.zip", [feedRow()])
    plan = buildFeedImportPlan(bundle)
    first = importFeedPlan(db, plan)
    second = importFeedPlan(db, plan)
    record = plan.records[0]

    assert first.insertedCount == 1
    assert first.updatedCount == 0
    assert first.classificationCount == 1
    assert second.insertedCount == 0
    assert second.updatedCount == 1
    row = db.execute(
        text('SELECT "typeRef"::text, name, knowledge FROM things WHERE id = :id'),
        {"id": record.objectRef},
    ).one()
    assert row.typeRef.startswith("syn://type/")
    assert row.name == "Example Channel"
    assert row.knowledge["description"]
    assert row.knowledge["sourceDataset"] == "global_data_sources_master_2326.csv"
    assert db.scalar(
        text('SELECT count(*) FROM "objectTypes" WHERE "objectRef" = :id'),
        {"id": record.objectRef},
    ) == 0
    assert db.scalar(
        text(
            'SELECT "objectHasType"('
            'CAST(:object AS "objectRef"), '
            "CAST('syn://type/rss-feed' AS \"typeRef\"))"
        ),
        {"object": record.objectRef},
    ) is True
    assert db.scalar(
        text(
            'SELECT "objectHasType"('
            'CAST(:object AS "objectRef"), '
            "CAST('syn://type/information-object' AS \"typeRef\"))"
        ),
        {"object": record.objectRef},
    ) is True


def test_question_seed_keeps_original_definition(db: Connection) -> None:
    row = db.execute(
        text(
            "SELECT name, knowledge ->> 'definition' AS definition "
            "FROM types WHERE id = 'syn://type/question'"
        )
    ).one()
    assert row.name == "question"
    assert row.definition == (
        "A proposition which can be answered, either yes or no, or with any other "
        "kind of response."
    )
    assert db.scalar(
        text(
            'SELECT count(*) FROM "typeParents" '
            "WHERE \"childRef\" = 'syn://type/question' "
            "AND \"parentRef\" = 'syn://type/proposition'"
        )
    ) == 1


def test_seeded_type_can_have_multiple_parents(db: Connection) -> None:
    parents = set(
        db.scalars(
            text(
                'SELECT "parentRef"::text FROM "typeParents" '
                "WHERE \"childRef\" = 'syn://type/company'"
            )
        )
    )
    assert parents == {
        "syn://type/organization",
        "syn://type/legal-entity",
        "syn://type/economic-agent",
    }


def test_type_parent_materializes_missing_type_placeholders(db: Connection) -> None:
    child = localRef("type", token("child-placeholder"))
    parent = localRef("type", token("parent-placeholder"))
    db.execute(
        text(
            'INSERT INTO "typeParents" ("childRef", "parentRef") '
            "VALUES (:child, :parent)"
        ),
        {"child": child, "parent": parent},
    )
    assert db.scalar(text("SELECT count(*) FROM types WHERE id IN (:child, :parent)"), {"child": child, "parent": parent}) == 2


def test_type_parent_rejects_self_parent(db: Connection) -> None:
    typeRef = localRef("type", token("self-parent"))
    expectDatabaseError(
        db,
        lambda: db.execute(
            text(
                'INSERT INTO "typeParents" ("childRef", "parentRef") '
                "VALUES (:type, :type)"
            ),
            {"type": typeRef},
        ),
    )


def test_type_parent_rejects_cycle(db: Connection) -> None:
    first = localRef("type", token("cycle-first"))
    second = localRef("type", token("cycle-second"))
    db.execute(
        text(
            'INSERT INTO "typeParents" ("childRef", "parentRef") '
            "VALUES (:first, 'syn://type/thing'), (:second, :first)"
        ),
        {"first": first, "second": second},
    )
    error = expectDatabaseError(
        db,
        lambda: db.execute(
            text(
                'INSERT INTO "typeParents" ("childRef", "parentRef") '
                "VALUES (:first, :second)"
            ),
            {"first": first, "second": second},
        ),
    )
    assert "cycle" in str(error).lower()


def test_type_parent_endpoints_are_immutable(db: Connection) -> None:
    child = localRef("type", token("immutable-child"))
    parent = localRef("type", token("immutable-parent"))
    db.execute(
        text(
            'INSERT INTO "typeParents" ("childRef", "parentRef") '
            "VALUES (:child, :parent)"
        ),
        {"child": child, "parent": parent},
    )
    error = expectDatabaseError(
        db,
        lambda: db.execute(
            text(
                'UPDATE "typeParents" SET "parentRef" = \'syn://type/thing\' '
                'WHERE "childRef" = :child AND "parentRef" = :parent'
            ),
            {"child": child, "parent": parent},
        ),
    )
    assert "immutable" in str(error).lower()


def test_inherited_type_membership_crosses_multiple_parents(db: Connection) -> None:
    objectRef = localRef("thing", token("inherited-type"))
    db.execute(
        text(
            'INSERT INTO things (id, "typeRef", name) '
            "VALUES (CAST(:object AS \"thingRef\"), 'syn://type/company', :name)"
        ),
        {"object": objectRef, "name": "Inherited type"},
    )
    assert db.scalar(
        text(
            'SELECT "objectHasType"('
            'CAST(:object AS "objectRef"), '
            "CAST('syn://type/organization' AS \"typeRef\"))"
        ),
        {"object": objectRef},
    ) is True
    assert db.scalar(
        text(
            'SELECT "objectHasType"('
            'CAST(:object AS "objectRef"), '
            "CAST('syn://type/legal-entity' AS \"typeRef\"))"
        ),
        {"object": objectRef},
    ) is True
    assert db.scalar(
        text(
            'SELECT "objectHasType"('
            'CAST(:object AS "objectRef"), '
            "CAST('syn://type/animal-organism' AS \"typeRef\"))"
        ),
        {"object": objectRef},
    ) is False


def test_assert_object_has_type_accepts_inherited_type(db: Connection) -> None:
    objectRef = localRef("thing", token("assert-inherited"))
    db.execute(
        text(
            'INSERT INTO things (id, "typeRef", name) '
            "VALUES (CAST(:object AS \"thingRef\"), 'syn://type/question', :name)"
        ),
        {"object": objectRef, "name": "Inherited assertion"},
    )
    db.execute(
        text(
            'SELECT "assertObjectHasType"('
            'CAST(:object AS "objectRef"), CAST(\'proposition\' AS slug))'
        ),
        {"object": objectRef},
    )


def test_delete_guard_covers_type_parent_edges(db: Connection) -> None:
    child = localRef("type", token("delete-parent-child"))
    parent = localRef("type", token("delete-parent-parent"))
    db.execute(
        text(
            'INSERT INTO "typeParents" ("childRef", "parentRef") '
            "VALUES (:child, :parent)"
        ),
        {"child": child, "parent": parent},
    )
    expectDatabaseError(
        db,
        lambda: db.execute(text("DELETE FROM types WHERE id = :parent"), {"parent": parent}),
    )
