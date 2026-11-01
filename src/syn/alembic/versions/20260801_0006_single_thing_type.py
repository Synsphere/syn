"""Give every thing exactly one direct type."""

from alembic import op


revision = "20260801_0006"
down_revision = "20260730_0005"
branch_labels = None
depends_on = None


RESOLVE_TYPE_INTERSECTION = r'''
CREATE OR REPLACE FUNCTION "resolveTypeIntersection"(
    "candidateParents" "typeRef"[]
)
RETURNS "typeRef"
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    "rawParents" "typeRef"[];
    "normalizedParents" "typeRef"[];
    "parentRef" "typeRef";
    "resolvedRef" "typeRef";
    "generatedName" text;
    "baseSlug" text;
    "candidateSlug" text;
    "parentKey" text;
    "attempt" integer := 0;
BEGIN
    SELECT array_agg(parent ORDER BY parent::text)
    INTO "rawParents"
    FROM (
        SELECT DISTINCT value AS parent
        FROM unnest("candidateParents") value
    ) unique_parents;

    IF coalesce(cardinality("rawParents"), 0) = 0 THEN
        RAISE EXCEPTION 'a type intersection requires at least one parent'
            USING ERRCODE = 'check_violation';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended('syn.typeParents', 0));

    FOREACH "parentRef" IN ARRAY "rawParents" LOOP
        PERFORM "ensureLocalObject"("parentRef"::"objectRef");
    END LOOP;

    -- Retain only the most specific supplied parents. This converts legacy
    -- classifications into the antichain required by Type.replaceParents.
    SELECT array_agg(parent ORDER BY parent::text)
    INTO "normalizedParents"
    FROM unnest("rawParents") parent
    WHERE NOT EXISTS (
        SELECT 1
        FROM unnest("rawParents") more_specific
        JOIN "typeAncestry" ancestry
          ON ancestry."descendantRef" = more_specific
         AND ancestry."ancestorRef" = parent
        WHERE more_specific <> parent
    );

    IF cardinality("normalizedParents") = 1 THEN
        RETURN "normalizedParents"[1];
    END IF;

    IF EXISTS (
        SELECT 1
        FROM unnest("normalizedParents") descendant
        JOIN unnest("normalizedParents") ancestor ON ancestor <> descendant
        JOIN "typeAncestry" ancestry
          ON ancestry."descendantRef" = descendant
         AND ancestry."ancestorRef" = ancestor
    ) THEN
        RAISE EXCEPTION 'normalized type intersection contains ancestor parents'
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT grouped."childRef"
    INTO "resolvedRef"
    FROM (
        SELECT
            edge."childRef",
            array_agg(edge."parentRef" ORDER BY edge."parentRef"::text) AS parents
        FROM "typeParents" edge
        GROUP BY edge."childRef"
    ) grouped
    WHERE grouped.parents = "normalizedParents"
    ORDER BY grouped."childRef"::text
    LIMIT 1;

    IF "resolvedRef" IS NOT NULL THEN
        RETURN "resolvedRef";
    END IF;

    SELECT
        string_agg(
            CASE
                WHEN item.ordinality = 1 THEN parent.name
                ELSE 'And' || upper(left(parent.name, 1)) || substring(parent.name FROM 2)
            END,
            '' ORDER BY item.ordinality
        ),
        string_agg(
            "objectRefSlug"(item.reference::"objectRef")::text,
            '-and-' ORDER BY item.ordinality
        ),
        string_agg(item.reference::text, '|' ORDER BY item.ordinality)
    INTO "generatedName", "baseSlug", "parentKey"
    FROM unnest("normalizedParents") WITH ORDINALITY item(reference, ordinality)
    JOIN types parent ON parent.id = item.reference;

    LOOP
        IF "attempt" = 0 AND char_length("baseSlug") <= 255 THEN
            "candidateSlug" := "baseSlug";
        ELSE
            "candidateSlug" :=
                rtrim(left("baseSlug", 242), '-_') || '-' ||
                substring(md5("parentKey" || ':' || "attempt"::text) FROM 1 FOR 12);
        END IF;

        "resolvedRef" :=
            "makeObjectRef"('type', "candidateSlug"::slug)::"typeRef";
        EXIT WHEN NOT EXISTS (SELECT 1 FROM types WHERE id = "resolvedRef");
        "attempt" := "attempt" + 1;
    END LOOP;

    INSERT INTO types (id, name, knowledge)
    VALUES (
        "resolvedRef",
        "generatedName",
        jsonb_build_object(
            'description',
            "generatedName" ||
                ' is a generated intersection type whose instances satisfy all listed parent types.',
            'generatedType', true,
            'generationKind', 'typeIntersection',
            'parentRefs', to_jsonb("normalizedParents")
        )
    );

    INSERT INTO "typeParents" ("childRef", "parentRef")
    SELECT "resolvedRef", parent
    FROM unnest("normalizedParents") parent;

    RETURN "resolvedRef";
END;
$$
'''


OBJECT_TYPES_INHERITED_WITH_THINGS = r'''
CREATE OR REPLACE VIEW "objectTypesInherited" AS
WITH direct_types("objectRef", "typeRef") AS (
    SELECT thing.id::"objectRef", thing."typeRef"
    FROM things thing
    UNION ALL
    SELECT direct."objectRef", direct."typeRef"
    FROM "objectTypes" direct
)
SELECT direct_types."objectRef", direct_types."typeRef", 0::integer AS depth
FROM direct_types
UNION
SELECT direct_types."objectRef", ancestry."ancestorRef" AS "typeRef", ancestry.depth
FROM direct_types
JOIN "typeAncestry" ancestry
  ON ancestry."descendantRef" = direct_types."typeRef"
'''


ENSURE_LOCAL_OBJECT_WITH_THING_TYPE = r'''
CREATE OR REPLACE FUNCTION "ensureLocalObject"("candidateRef" "objectRef")
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    "objectTable" text := "objectRefTable"("candidateRef");
    "objectSlug" slug := "objectRefSlug"("candidateRef");
BEGIN
    IF "candidateRef"::text !~ '^syn://' THEN
        RETURN;
    END IF;

    CASE "objectTable"
        WHEN 'type' THEN
            INSERT INTO types (id, name)
            VALUES ("candidateRef"::"typeRef", "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
        WHEN 'thing' THEN
            INSERT INTO things (id, "typeRef", name)
            VALUES (
                "candidateRef"::"thingRef",
                'syn://type/thing'::"typeRef",
                "objectSlug"::text
            )
            ON CONFLICT (id) DO NOTHING;
        WHEN 'event' THEN
            INSERT INTO events (id, name)
            VALUES ("candidateRef"::"eventRef", "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
        WHEN 'ember' THEN
            INSERT INTO embers (id, name)
            VALUES ("candidateRef"::"emberRef", "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
        WHEN 'cog' THEN
            INSERT INTO cogs (id, name)
            VALUES ("candidateRef"::"cogRef", "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
        WHEN 'enigma' THEN
            INSERT INTO enigmas (id, name)
            VALUES ("candidateRef"::"enigmaRef", "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
        WHEN 'pondle' THEN
            INSERT INTO pondles (id, name)
            VALUES ("candidateRef"::"pondleRef", "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
        WHEN 'snug' THEN
            INSERT INTO snugs (id, name)
            VALUES ("candidateRef"::"snugRef", "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
        WHEN 'om' THEN
            INSERT INTO oms (id, name)
            VALUES ("candidateRef"::"omRef", "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
        ELSE
            RAISE EXCEPTION 'unsupported local ObjectRef: %', "candidateRef"
                USING ERRCODE = 'check_violation';
    END CASE;
END;
$$
'''


PREVENT_REFERENCED_DELETE_WITH_THING_TYPE = r'''
CREATE OR REPLACE FUNCTION "preventReferencedLocalDelete"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM "objectTypes"
               WHERE "objectRef" = OLD.id OR "typeRef"::"objectRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM things
                  WHERE "typeRef"::"objectRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM "typeParents"
                  WHERE "childRef"::"objectRef" = OLD.id
                     OR "parentRef"::"objectRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM "eventTargets"
                  WHERE "eventRef"::"objectRef" = OLD.id OR "targetRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM "objectRelationships"
                  WHERE "sourceRef" = OLD.id OR "targetRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM "objectChronicle"
                  WHERE "objectRef" = OLD.id OR "eventRef"::"objectRef" = OLD.id)
    THEN
        RAISE EXCEPTION 'cannot delete referenced local ObjectRef %', OLD.id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN OLD;
END;
$$
'''


def upgrade() -> None:
    statements = (
        "SELECT pg_advisory_xact_lock(hashtextextended('syn.typeParents', 0))",
        """
        ALTER TABLE things
        ADD COLUMN "typeRef" "typeRef"
        DEFAULT 'syn://type/thing'::"typeRef"
        """,
        RESOLVE_TYPE_INTERSECTION,
        """
        CREATE TEMP TABLE "thingTypeParentSets" ON COMMIT DROP AS
        WITH normalized AS (
            SELECT direct."objectRef", direct."typeRef"
            FROM "objectTypes" direct
            WHERE direct."objectRef"::text ~ '^syn://thing/'
              AND NOT EXISTS (
                  SELECT 1
                  FROM "objectTypes" more_specific
                  JOIN "typeAncestry" ancestry
                    ON ancestry."descendantRef" = more_specific."typeRef"
                   AND ancestry."ancestorRef" = direct."typeRef"
                  WHERE more_specific."objectRef" = direct."objectRef"
                    AND more_specific."typeRef" <> direct."typeRef"
              )
        )
        SELECT
            normalized."objectRef",
            array_agg(normalized."typeRef" ORDER BY normalized."typeRef"::text) AS parents
        FROM normalized
        GROUP BY normalized."objectRef"
        """,
        """
        UPDATE things thing
        SET "typeRef" = parent_sets.parents[1]
        FROM "thingTypeParentSets" parent_sets
        WHERE thing.id::"objectRef" = parent_sets."objectRef"
          AND cardinality(parent_sets.parents) = 1
        """,
        """
        CREATE TEMP TABLE "thingTypeIntersectionGroups" ON COMMIT DROP AS
        SELECT DISTINCT parent_sets.parents, NULL::"typeRef" AS "resolvedRef"
        FROM "thingTypeParentSets" parent_sets
        WHERE cardinality(parent_sets.parents) > 1
        """,
        """
        UPDATE "thingTypeIntersectionGroups"
        SET "resolvedRef" = "resolveTypeIntersection"(parents)
        """,
        """
        UPDATE things thing
        SET "typeRef" = groups."resolvedRef"
        FROM "thingTypeParentSets" parent_sets
        JOIN "thingTypeIntersectionGroups" groups
          ON groups.parents = parent_sets.parents
        WHERE thing.id::"objectRef" = parent_sets."objectRef"
        """,
        # ON COMMIT DROP is too late when all revisions share one transaction:
        # the later Credential domain check cannot validate a domain-array column.
        # These scratch tables are no longer used after the preceding UPDATE.
        'DROP TABLE pg_temp."thingTypeIntersectionGroups"',
        'DROP TABLE pg_temp."thingTypeParentSets"',
        """
        DELETE FROM "objectTypes" direct
        USING things thing
        WHERE direct."objectRef" = thing.id::"objectRef"
        """,
        """
        ALTER TABLE "objectTypes"
        ADD CONSTRAINT "objectTypesNoThingCheck"
        CHECK ("objectRef"::text !~ '^syn://thing/')
        """,
        "ALTER TABLE things ALTER COLUMN \"typeRef\" DROP DEFAULT",
        "ALTER TABLE things ALTER COLUMN \"typeRef\" SET NOT NULL",
        "CREATE INDEX \"thingsTypeRefIdx\" ON things (\"typeRef\", id)",
        ENSURE_LOCAL_OBJECT_WITH_THING_TYPE,
        """
        CREATE TRIGGER "thingsRequireType"
        BEFORE INSERT OR UPDATE OF "typeRef" ON things
        FOR EACH ROW EXECUTE FUNCTION "requireRegistryKind"('typeRef', 'type')
        """,
        OBJECT_TYPES_INHERITED_WITH_THINGS,
        PREVENT_REFERENCED_DELETE_WITH_THING_TYPE,
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    statements = (
        'DROP TRIGGER IF EXISTS "thingsRequireType" ON things',
        'DROP INDEX IF EXISTS "thingsTypeRefIdx"',
        'ALTER TABLE "objectTypes" DROP CONSTRAINT IF EXISTS "objectTypesNoThingCheck"',
        """
        INSERT INTO "objectTypes" ("objectRef", "typeRef")
        SELECT id::"objectRef", "typeRef"
        FROM things
        ON CONFLICT ("objectRef", "typeRef") DO NOTHING
        """,
        """
        CREATE OR REPLACE VIEW "objectTypesInherited" AS
        SELECT "objectRef", "typeRef", 0::integer AS depth
        FROM "objectTypes"
        UNION
        SELECT direct."objectRef", ancestry."ancestorRef" AS "typeRef", ancestry.depth
        FROM "objectTypes" direct
        JOIN "typeAncestry" ancestry
          ON ancestry."descendantRef" = direct."typeRef"
        """,
        'ALTER TABLE things DROP COLUMN "typeRef"',
        r'''
        CREATE OR REPLACE FUNCTION "ensureLocalObject"("candidateRef" "objectRef")
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        DECLARE
            "objectTable" text := "objectRefTable"("candidateRef");
            "objectSlug" slug := "objectRefSlug"("candidateRef");
        BEGIN
            IF "candidateRef"::text !~ '^syn://' THEN RETURN; END IF;
            CASE "objectTable"
                WHEN 'type' THEN INSERT INTO types (id, name) VALUES ("candidateRef"::"typeRef", "objectSlug"::text) ON CONFLICT (id) DO NOTHING;
                WHEN 'thing' THEN INSERT INTO things (id, name) VALUES ("candidateRef"::"thingRef", "objectSlug"::text) ON CONFLICT (id) DO NOTHING;
                WHEN 'event' THEN INSERT INTO events (id, name) VALUES ("candidateRef"::"eventRef", "objectSlug"::text) ON CONFLICT (id) DO NOTHING;
                WHEN 'ember' THEN INSERT INTO embers (id, name) VALUES ("candidateRef"::"emberRef", "objectSlug"::text) ON CONFLICT (id) DO NOTHING;
                WHEN 'cog' THEN INSERT INTO cogs (id, name) VALUES ("candidateRef"::"cogRef", "objectSlug"::text) ON CONFLICT (id) DO NOTHING;
                WHEN 'enigma' THEN INSERT INTO enigmas (id, name) VALUES ("candidateRef"::"enigmaRef", "objectSlug"::text) ON CONFLICT (id) DO NOTHING;
                WHEN 'pondle' THEN INSERT INTO pondles (id, name) VALUES ("candidateRef"::"pondleRef", "objectSlug"::text) ON CONFLICT (id) DO NOTHING;
                WHEN 'snug' THEN INSERT INTO snugs (id, name) VALUES ("candidateRef"::"snugRef", "objectSlug"::text) ON CONFLICT (id) DO NOTHING;
                WHEN 'om' THEN INSERT INTO oms (id, name) VALUES ("candidateRef"::"omRef", "objectSlug"::text) ON CONFLICT (id) DO NOTHING;
                ELSE RAISE EXCEPTION 'unsupported local ObjectRef: %', "candidateRef" USING ERRCODE = 'check_violation';
            END CASE;
        END;
        $$
        ''',
        r'''
        CREATE OR REPLACE FUNCTION "preventReferencedLocalDelete"()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM "objectTypes" WHERE "objectRef" = OLD.id OR "typeRef"::"objectRef" = OLD.id)
               OR EXISTS (SELECT 1 FROM "typeParents" WHERE "childRef"::"objectRef" = OLD.id OR "parentRef"::"objectRef" = OLD.id)
               OR EXISTS (SELECT 1 FROM "eventTargets" WHERE "eventRef"::"objectRef" = OLD.id OR "targetRef" = OLD.id)
               OR EXISTS (SELECT 1 FROM "objectRelationships" WHERE "sourceRef" = OLD.id OR "targetRef" = OLD.id)
               OR EXISTS (SELECT 1 FROM "objectChronicle" WHERE "objectRef" = OLD.id OR "eventRef"::"objectRef" = OLD.id)
            THEN
                RAISE EXCEPTION 'cannot delete referenced local ObjectRef %', OLD.id USING ERRCODE = 'foreign_key_violation';
            END IF;
            RETURN OLD;
        END;
        $$
        ''',
        'DROP FUNCTION IF EXISTS "resolveTypeIntersection"("typeRef"[])',
    )
    for statement in statements:
        op.execute(statement)
