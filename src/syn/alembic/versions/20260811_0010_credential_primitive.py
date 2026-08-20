"""Add the owner-private Credential primitive."""

from alembic import op


revision = "20260811_0010"
down_revision = "20260801_0009"
branch_labels = None
depends_on = None


OBJECT_REF_CHECK = r'''
ALTER DOMAIN "objectRef" ADD CONSTRAINT "objectRef_check" CHECK (
    VALUE <> ''
    AND VALUE !~ '[[:space:][:cntrl:]]'
    AND regexp_replace(VALUE, '%[0-9A-Fa-f]{2}', '', 'g') !~ '%'
    AND (
        VALUE !~ '^syn://'
        OR VALUE ~ '^syn://(type|thing|event|ember|cog|credential|enigma|pondle|snug|om)/[a-z0-9]+(?:[-_][a-z0-9]+)*$'
    )
)
'''


OBJECT_REF_CHECK_DOWNGRADE = OBJECT_REF_CHECK.replace("|credential", "")


DROP_OBJECT_REF_CHECK = r'''
DO $$
DECLARE
    "constraintName" name;
BEGIN
    SELECT constraint_name
    INTO "constraintName"
    FROM information_schema.domain_constraints
    WHERE domain_schema = current_schema()
      AND domain_name = 'objectRef'
      AND constraint_name LIKE 'objectRef%check%'
    ORDER BY constraint_name
    LIMIT 1;

    IF "constraintName" IS NULL THEN
        RAISE EXCEPTION 'could not find the objectRef domain check constraint';
    END IF;
    EXECUTE format('ALTER DOMAIN "objectRef" DROP CONSTRAINT %I', "constraintName");
END;
$$
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
        WHEN 'credential' THEN
            INSERT INTO credentials (id, "typeRef", name)
            VALUES ("candidateRef"::"credentialRef", 'syn://type/credential', "objectSlug"::text)
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


ENSURE_LOCAL_OBJECT_DOWNGRADE = ENSURE_LOCAL_OBJECT.replace(
    '''        WHEN 'credential' THEN
            INSERT INTO credentials (id, "typeRef", name)
            VALUES ("candidateRef"::"credentialRef", 'syn://type/credential', "objectSlug"::text)
            ON CONFLICT (id) DO NOTHING;
''',
    "",
)


OBJECT_TYPE_ANCESTRY = r'''
CREATE OR REPLACE VIEW "objectTypeAncestry" AS
WITH direct_types("objectRef", "typeRef") AS (
    SELECT id::"objectRef", "typeRef" FROM things
    UNION ALL SELECT id::"objectRef", "typeRef" FROM events
    UNION ALL SELECT id::"objectRef", "typeRef" FROM embers
    UNION ALL SELECT id::"objectRef", "typeRef" FROM cogs
    UNION ALL SELECT id::"objectRef", "typeRef" FROM credentials
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


OBJECT_TYPE_ANCESTRY_DOWNGRADE = OBJECT_TYPE_ANCESTRY.replace(
    '    UNION ALL SELECT id::"objectRef", "typeRef" FROM credentials\n',
    "",
)


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
       OR EXISTS (SELECT 1 FROM credentials WHERE "typeRef"::"objectRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM enigmas WHERE "typeRef"::"objectRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM pondles WHERE "typeRef"::"objectRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM snugs WHERE "typeRef"::"objectRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM oms WHERE "typeRef"::"objectRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM "typeParents"
                  WHERE "childRef"::"objectRef" = OLD.id
                     OR "parentRef"::"objectRef" = OLD.id)
       OR EXISTS (SELECT 1 FROM "typeProperties"
                  WHERE "type"::"objectRef" = OLD.id
                     OR "propertyTypeRef"::"objectRef" = OLD.id)
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


PREVENT_REFERENCED_DELETE_DOWNGRADE = PREVENT_REFERENCED_DELETE.replace(
    '       OR EXISTS (SELECT 1 FROM credentials WHERE "typeRef"::"objectRef" = OLD.id)\n',
    "",
)


def upgrade() -> None:
    op.execute("SELECT pg_advisory_xact_lock(hashtextextended('syn.typeParents', 0))")
    op.execute(DROP_OBJECT_REF_CHECK)
    op.execute(OBJECT_REF_CHECK)
    op.execute(
        r'''
        CREATE DOMAIN "credentialRef" AS "objectRef"
            CHECK (VALUE::text ~ '^syn://credential/')
        '''
    )
    op.execute(
        r'''
        CREATE TABLE credentials (
            id "credentialRef" PRIMARY KEY,
            "typeRef" "typeRef" NOT NULL,
            name text NOT NULL,
            locality integer,
            "publicKeyHash" bytea,
            "privateData" bytea,
            stats jsonb,
            "statsUpdated" timestamptz,
            knowledge jsonb,
            "knowledgeUpdated" timestamptz,
            confidence numeric,
            "confidenceUpdated" timestamptz,
            CONSTRAINT "credentialsIdCheck"
                CHECK (id::text ~ '^syn://credential/'),
            CONSTRAINT "credentialsConfidenceCheck"
                CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
            CONSTRAINT "credentialsPublicKeyHashNotEmptyCheck"
                CHECK ("publicKeyHash" IS NULL OR octet_length("publicKeyHash") > 0),
            CONSTRAINT "credentialsPrivateDataNotEmptyCheck"
                CHECK ("privateData" IS NULL OR octet_length("privateData") > 0),
            CONSTRAINT "credentialsPublicKeyHashKey" UNIQUE ("publicKeyHash")
        )
        '''
    )
    op.execute(
        '''COMMENT ON COLUMN credentials."privateData" IS
        'Owner-private opaque bytes. Never expose through public APIs.' ''')
    op.execute('CREATE INDEX "credentialsTypeRefIdx" ON credentials ("typeRef", id)')
    op.execute('CREATE INDEX "credentialsStatsGinIdx" ON credentials USING gin (stats)')
    op.execute(
        'CREATE TRIGGER "credentialsPreventIdChange" '
        'BEFORE UPDATE OF id ON credentials '
        'FOR EACH ROW EXECUTE FUNCTION "preventObjectRefChange"()'
    )
    op.execute(
        'CREATE TRIGGER "credentialsMaintainRegistry" '
        'AFTER INSERT OR DELETE ON credentials '
        "FOR EACH ROW EXECUTE FUNCTION \"maintainObjectRegistry\"('credential')"
    )
    op.execute(
        'CREATE TRIGGER "credentialsPreventTruncate" '
        'BEFORE TRUNCATE ON credentials '
        'FOR EACH STATEMENT EXECUTE FUNCTION "preventRegistryBypass"()'
    )
    op.execute(
        'CREATE TRIGGER "credentialsRequireType" '
        'BEFORE INSERT OR UPDATE OF "typeRef" ON credentials '
        "FOR EACH ROW EXECUTE FUNCTION \"requireRegistryKind\"('typeRef', 'type')"
    )
    op.execute(
        r'''
        INSERT INTO types (id, name, knowledge) VALUES (
            'syn://type/credential'::"typeRef",
            'credential',
            jsonb_build_object(
                'definition',
                'The root type for owner-held credentials identified by a public-key hash and containing owner-private data.',
                'module', 'syn',
                'ontology', 'syn-core',
                'ontologyVersion', 4
            )
        )
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            knowledge = coalesce(types.knowledge, '{}'::jsonb) || EXCLUDED.knowledge
        '''
    )
    op.execute(
        "UPDATE types SET knowledge = jsonb_set(knowledge, '{ontologyVersion}', '4') "
        "WHERE knowledge->>'ontology' = 'syn-core'"
    )
    op.execute(
        r'''
        INSERT INTO "typeParents" ("childRef", "parentRef") VALUES
            ('syn://type/credential'::"typeRef", 'syn://type/information-object'::"typeRef")
        ON CONFLICT ("childRef", "parentRef") DO NOTHING
        '''
    )
    op.execute(ENSURE_LOCAL_OBJECT)
    op.execute(OBJECT_TYPE_ANCESTRY)
    op.execute(PREVENT_REFERENCED_DELETE)


def downgrade() -> None:
    op.execute("SELECT pg_advisory_xact_lock(hashtextextended('syn.typeParents', 0))")
    op.execute('DELETE FROM credentials')
    op.execute(OBJECT_TYPE_ANCESTRY_DOWNGRADE)
    op.execute(ENSURE_LOCAL_OBJECT_DOWNGRADE)
    op.execute(PREVENT_REFERENCED_DELETE_DOWNGRADE)
    op.execute(
        r'''
        DELETE FROM "typeParents"
        WHERE "childRef" = 'syn://type/credential'::"typeRef"
          AND "parentRef" = 'syn://type/information-object'::"typeRef"
        '''
    )
    op.execute("DELETE FROM types WHERE id = 'syn://type/credential'::\"typeRef\"")
    op.execute(
        "UPDATE types SET knowledge = jsonb_set(knowledge, '{ontologyVersion}', '3') "
        "WHERE knowledge->>'ontology' = 'syn-core'"
    )
    op.execute('DROP TABLE credentials')
    op.execute('DROP DOMAIN "credentialRef"')
    op.execute(DROP_OBJECT_REF_CHECK)
    op.execute(OBJECT_REF_CHECK_DOWNGRADE)
