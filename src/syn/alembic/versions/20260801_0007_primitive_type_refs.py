"""Give every primitive exactly one direct type and retire objectTypes."""

from alembic import op


revision = "20260801_0007"
down_revision = "20260801_0006"
branch_labels = None
depends_on = None


ROOT_TYPES = r'''
INSERT INTO types (id, name, knowledge) VALUES
    ('syn://type/cog'::"typeRef", 'cog',
     '{"definition":"The root type for computational processes represented as Cogs.","module":"syn","ontology":"syn-core","ontologyVersion": 3}'::jsonb),
    ('syn://type/ember'::"typeRef", 'ember',
     '{"definition":"The root type for known pieces of information represented as Embers.","module":"syn","ontology":"syn-core","ontologyVersion": 3}'::jsonb),
    ('syn://type/enigma'::"typeRef", 'enigma',
     '{"definition":"The root type for propositions represented as Enigmas that can receive a response.","module":"syn","ontology":"syn-core","ontologyVersion": 3}'::jsonb),
    ('syn://type/event'::"typeRef", 'event',
     '{"module":"occurrent","ontology":"syn-core","ontologyVersion": 3}'::jsonb),
    ('syn://type/om'::"typeRef", 'om',
     '{"definition":"The root type for units of Karma represented as Om.","module":"syn","ontology":"syn-core","ontologyVersion": 3}'::jsonb),
    ('syn://type/pondle'::"typeRef", 'pondle',
     '{"definition":"The root type for objective evaluations represented as Pondles.","module":"syn","ontology":"syn-core","ontologyVersion": 3}'::jsonb),
    ('syn://type/snug'::"typeRef", 'snug',
     '{"definition":"The root type for investments and emotional attachments represented as Snugs.","module":"syn","ontology":"syn-core","ontologyVersion": 3}'::jsonb)
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    knowledge = coalesce(types.knowledge, '{}'::jsonb) || EXCLUDED.knowledge
'''


ROOT_TYPE_EDGES = r'''
DELETE FROM "typeParents"
WHERE "childRef" = 'syn://type/observation-result'::"typeRef"
  AND "parentRef" = 'syn://type/information-object'::"typeRef";

INSERT INTO "typeParents" ("childRef", "parentRef") VALUES
    ('syn://type/cog'::"typeRef", 'syn://type/computational-process'::"typeRef"),
    ('syn://type/ember'::"typeRef", 'syn://type/knowledge-record'::"typeRef"),
    ('syn://type/enigma'::"typeRef", 'syn://type/question'::"typeRef"),
    ('syn://type/event'::"typeRef", 'syn://type/occurrent-thing'::"typeRef"),
    ('syn://type/om'::"typeRef", 'syn://type/karma-unit'::"typeRef"),
    ('syn://type/pondle'::"typeRef", 'syn://type/evaluation-record'::"typeRef"),
    ('syn://type/snug'::"typeRef", 'syn://type/attachment-record'::"typeRef"),
    ('syn://type/observation-result'::"typeRef", 'syn://type/ember'::"typeRef")
ON CONFLICT ("childRef", "parentRef") DO NOTHING
'''


OBJECT_TYPES_INHERITED = r'''
CREATE OR REPLACE VIEW "objectTypesInherited" AS
WITH direct_types("objectRef", "typeRef") AS (
    SELECT id::"objectRef", "typeRef" FROM things
    UNION ALL SELECT id::"objectRef", "typeRef" FROM events
    UNION ALL SELECT id::"objectRef", "typeRef" FROM embers
    UNION ALL SELECT id::"objectRef", "typeRef" FROM cogs
    UNION ALL SELECT id::"objectRef", "typeRef" FROM enigmas
    UNION ALL SELECT id::"objectRef", "typeRef" FROM pondles
    UNION ALL SELECT id::"objectRef", "typeRef" FROM snugs
    UNION ALL SELECT id::"objectRef", "typeRef" FROM oms
)
SELECT direct_types."objectRef", direct_types."typeRef", 0::integer AS depth
FROM direct_types
UNION
SELECT direct_types."objectRef", ancestry."ancestorRef" AS "typeRef", ancestry.depth
FROM direct_types
JOIN "typeAncestry" ancestry
  ON ancestry."descendantRef" = direct_types."typeRef"
'''


ENSURE_LOCAL_OBJECT = r'''
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
            VALUES ("candidateRef"::"thingRef", 'syn://type/thing', "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
        WHEN 'event' THEN
            INSERT INTO events (id, "typeRef", name)
            VALUES ("candidateRef"::"eventRef", 'syn://type/event', "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
        WHEN 'ember' THEN
            INSERT INTO embers (id, "typeRef", name)
            VALUES ("candidateRef"::"emberRef", 'syn://type/ember', "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
        WHEN 'cog' THEN
            INSERT INTO cogs (id, "typeRef", name)
            VALUES ("candidateRef"::"cogRef", 'syn://type/cog', "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
        WHEN 'enigma' THEN
            INSERT INTO enigmas (id, "typeRef", name)
            VALUES ("candidateRef"::"enigmaRef", 'syn://type/enigma', "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
        WHEN 'pondle' THEN
            INSERT INTO pondles (id, "typeRef", name)
            VALUES ("candidateRef"::"pondleRef", 'syn://type/pondle', "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
        WHEN 'snug' THEN
            INSERT INTO snugs (id, "typeRef", name)
            VALUES ("candidateRef"::"snugRef", 'syn://type/snug', "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
        WHEN 'om' THEN
            INSERT INTO oms (id, "typeRef", name)
            VALUES ("candidateRef"::"omRef", 'syn://type/om', "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
        ELSE
            RAISE EXCEPTION 'unsupported local ObjectRef: %', "candidateRef"
                USING ERRCODE = 'check_violation';
    END CASE;
END;
$$
'''


PREVENT_REFERENCED_DELETE = r'''
CREATE OR REPLACE FUNCTION "preventReferencedLocalDelete"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM things WHERE "typeRef"::"objectRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM events WHERE "typeRef"::"objectRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM embers WHERE "typeRef"::"objectRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM cogs WHERE "typeRef"::"objectRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM enigmas WHERE "typeRef"::"objectRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM pondles WHERE "typeRef"::"objectRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM snugs WHERE "typeRef"::"objectRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM oms WHERE "typeRef"::"objectRef" = OLD.id)
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


PRIMITIVES = (
    ("events", "event", "eventRef"),
    ("embers", "ember", "emberRef"),
    ("cogs", "cog", "cogRef"),
    ("enigmas", "enigma", "enigmaRef"),
    ("pondles", "pondle", "pondleRef"),
    ("snugs", "snug", "snugRef"),
    ("oms", "om", "omRef"),
)


def upgrade() -> None:
    op.execute("SELECT pg_advisory_xact_lock(hashtextextended('syn.typeParents', 0))")
    op.execute(ROOT_TYPES)
    op.execute(
        "UPDATE types SET knowledge = jsonb_set(knowledge, '{ontologyVersion}', '3') "
        "WHERE knowledge->>'ontology' = 'syn-core'"
    )
    op.execute(ROOT_TYPE_EDGES)

    for table, kind, _domain in PRIMITIVES:
        op.execute(
            f'ALTER TABLE {table} ADD COLUMN "typeRef" "typeRef" '
            f"DEFAULT 'syn://type/{kind}'::\"typeRef\""
        )

    op.execute(
        r'''
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM "objectTypes"
                WHERE "objectRefTable"("objectRef") NOT IN
                      ('event', 'ember', 'cog', 'enigma', 'pondle', 'snug', 'om')
            ) THEN
                RAISE EXCEPTION 'objectTypes contains an unsupported object kind'
                    USING ERRCODE = 'check_violation';
            END IF;
        END;
        $$
        '''
    )
    op.execute(
        r'''
        CREATE TEMP TABLE "primitiveTypeParentSets" ON COMMIT DROP AS
        WITH normalized AS (
            SELECT
                "objectRefTable"(direct."objectRef") AS kind,
                direct."objectRef",
                direct."typeRef"
            FROM "objectTypes" direct
            WHERE NOT EXISTS (
                SELECT 1
                FROM "objectTypes" more_specific
                JOIN "typeAncestry" ancestry
                  ON ancestry."descendantRef" = more_specific."typeRef"
                 AND ancestry."ancestorRef" = direct."typeRef"
                WHERE more_specific."objectRef" = direct."objectRef"
                  AND more_specific."typeRef" <> direct."typeRef"
            )
        )
        SELECT kind, "objectRef",
               array_agg("typeRef" ORDER BY "typeRef"::text) AS parents
        FROM normalized
        GROUP BY kind, "objectRef"
        '''
    )
    op.execute(
        r'''
        CREATE TEMP TABLE "primitiveTypeIntersectionGroups" ON COMMIT DROP AS
        SELECT DISTINCT parents, NULL::"typeRef" AS "resolvedRef"
        FROM "primitiveTypeParentSets"
        WHERE cardinality(parents) > 1
        '''
    )
    op.execute(
        'UPDATE "primitiveTypeIntersectionGroups" '
        'SET "resolvedRef" = "resolveTypeIntersection"(parents)'
    )

    for table, kind, _domain in PRIMITIVES:
        op.execute(
            f'''
            UPDATE {table} target
            SET "typeRef" = CASE
                WHEN cardinality(parent_sets.parents) = 1 THEN parent_sets.parents[1]
                ELSE groups."resolvedRef"
            END
            FROM "primitiveTypeParentSets" parent_sets
            LEFT JOIN "primitiveTypeIntersectionGroups" groups
              ON groups.parents = parent_sets.parents
            WHERE parent_sets.kind = '{kind}'
              AND target.id::"objectRef" = parent_sets."objectRef"
            '''
        )

    for table, kind, _domain in PRIMITIVES:
        op.execute(f'ALTER TABLE {table} ALTER COLUMN "typeRef" DROP DEFAULT')
        op.execute(f'ALTER TABLE {table} ALTER COLUMN "typeRef" SET NOT NULL')
        op.execute(f'CREATE INDEX "{table}TypeRefIdx" ON {table} ("typeRef", id)')
        op.execute(
            f'CREATE TRIGGER "{table}RequireType" '
            f'BEFORE INSERT OR UPDATE OF "typeRef" ON {table} '
            "FOR EACH ROW EXECUTE FUNCTION \"requireRegistryKind\"('typeRef', 'type')"
        )

    op.execute(ENSURE_LOCAL_OBJECT)
    op.execute(OBJECT_TYPES_INHERITED)
    op.execute(PREVENT_REFERENCED_DELETE)
    # A full bootstrap runs revisions in one transaction. Release completed
    # domain-array scratch tables before later ObjectRef constraint changes.
    op.execute('DROP TABLE pg_temp."primitiveTypeIntersectionGroups"')
    op.execute('DROP TABLE pg_temp."primitiveTypeParentSets"')
    op.execute('DROP TABLE "objectTypes"')


def downgrade() -> None:
    op.execute(
        r'''
        CREATE TABLE "objectTypes" (
            "objectRef" "objectRef" NOT NULL,
            "typeRef" "typeRef" NOT NULL,
            PRIMARY KEY ("objectRef", "typeRef"),
            CONSTRAINT "objectTypesNoThingCheck"
                CHECK ("objectRef"::text !~ '^syn://thing/')
        )
        '''
    )
    op.execute(
        'CREATE INDEX "objectTypesTypeIdx" '
        'ON "objectTypes" ("typeRef", "objectRef")'
    )
    op.execute(
        'CREATE TRIGGER "objectTypesRequireObjectIfLocal" '
        'BEFORE INSERT OR UPDATE OF "objectRef" ON "objectTypes" '
        "FOR EACH ROW EXECUTE FUNCTION \"requireLocalObjectIfSyn\"('objectRef')"
    )
    op.execute(
        'CREATE TRIGGER "objectTypesRequireType" '
        'BEFORE INSERT OR UPDATE OF "typeRef" ON "objectTypes" '
        "FOR EACH ROW EXECUTE FUNCTION \"requireRegistryKind\"('typeRef', 'type')"
    )

    for table, _kind, _domain in PRIMITIVES:
        op.execute(
            f'INSERT INTO "objectTypes" ("objectRef", "typeRef") '
            f'SELECT id::"objectRef", "typeRef" FROM {table}'
        )

    op.execute(
        r'''
        CREATE OR REPLACE VIEW "objectTypesInherited" AS
        WITH direct_types("objectRef", "typeRef") AS (
            SELECT id::"objectRef", "typeRef" FROM things
            UNION ALL
            SELECT "objectRef", "typeRef" FROM "objectTypes"
        )
        SELECT direct_types."objectRef", direct_types."typeRef", 0::integer AS depth
        FROM direct_types
        UNION
        SELECT direct_types."objectRef", ancestry."ancestorRef" AS "typeRef", ancestry.depth
        FROM direct_types
        JOIN "typeAncestry" ancestry
          ON ancestry."descendantRef" = direct_types."typeRef"
        '''
    )
    op.execute(
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
                WHEN 'thing' THEN INSERT INTO things (id, "typeRef", name) VALUES ("candidateRef"::"thingRef", 'syn://type/thing', "objectSlug"::text) ON CONFLICT (id) DO NOTHING;
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
        '''
    )
    op.execute(
        r'''
        CREATE OR REPLACE FUNCTION "preventReferencedLocalDelete"()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM "objectTypes" WHERE "objectRef" = OLD.id OR "typeRef"::"objectRef" = OLD.id)
               OR EXISTS (SELECT 1 FROM things WHERE "typeRef"::"objectRef" = OLD.id)
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
        '''
    )

    for table, _kind, _domain in PRIMITIVES:
        op.execute(f'DROP TRIGGER IF EXISTS "{table}RequireType" ON {table}')
        op.execute(f'DROP INDEX IF EXISTS "{table}TypeRefIdx"')
        op.execute(f'ALTER TABLE {table} DROP COLUMN "typeRef"')
