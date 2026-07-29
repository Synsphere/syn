BEGIN;

CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE DOMAIN slug AS varchar(255)
    CHECK (VALUE ~ '^[a-z0-9]+(?:[-_][a-z0-9]+)*$');

CREATE DOMAIN "objectRef" AS text
    CHECK (
        VALUE <> ''
        AND VALUE !~ '[[:space:][:cntrl:]]'
        AND regexp_replace(VALUE, '%[0-9A-Fa-f]{2}', '', 'g') !~ '%'
        AND (
            VALUE !~ '^syn://'
            OR VALUE ~ '^syn://(type|thing|event|ember|cog|enigma|pondle|snug|om)/[a-z0-9]+(?:[-_][a-z0-9]+)*$'
        )
    );

CREATE DOMAIN "typeRef" AS "objectRef"
    CHECK (VALUE::text ~ '^syn://type/');

CREATE DOMAIN "thingRef" AS "objectRef"
    CHECK (VALUE::text ~ '^syn://thing/');

CREATE DOMAIN "eventRef" AS "objectRef"
    CHECK (VALUE::text ~ '^syn://event/');

CREATE DOMAIN "emberRef" AS "objectRef"
    CHECK (VALUE::text ~ '^syn://ember/');

CREATE DOMAIN "cogRef" AS "objectRef"
    CHECK (VALUE::text ~ '^syn://cog/');

CREATE DOMAIN "enigmaRef" AS "objectRef"
    CHECK (VALUE::text ~ '^syn://enigma/');

CREATE DOMAIN "pondleRef" AS "objectRef"
    CHECK (VALUE::text ~ '^syn://pondle/');

CREATE DOMAIN "snugRef" AS "objectRef"
    CHECK (VALUE::text ~ '^syn://snug/');

CREATE DOMAIN "omRef" AS "objectRef"
    CHECK (VALUE::text ~ '^syn://om/');

-- Classification membership for these two domains is enforced by table
-- triggers at their points of use, not by domain checks.
CREATE DOMAIN "observationRef" AS "eventRef";
CREATE DOMAIN "balanceRef" AS "emberRef";

CREATE FUNCTION "makeObjectRef"("objectTable" text, "objectSlug" slug)
RETURNS "objectRef"
LANGUAGE sql
STRICT
IMMUTABLE
AS $$
    SELECT ('syn://' || "objectTable" || '/' || "objectSlug"::text)::"objectRef";
$$;

CREATE FUNCTION "objectRefTable"(value "objectRef")
RETURNS text
LANGUAGE sql
STRICT
IMMUTABLE
AS $$
    SELECT CASE
        WHEN value::text ~ '^syn://' THEN split_part(value::text, '/', 3)
        ELSE NULL
    END;
$$;

CREATE FUNCTION "objectRefSlug"(value "objectRef")
RETURNS slug
LANGUAGE sql
STRICT
IMMUTABLE
AS $$
    SELECT CASE
        WHEN value::text ~ '^syn://' THEN split_part(value::text, '/', 4)::slug
        ELSE NULL
    END;
$$;

CREATE FUNCTION "normalizeSlug"(value text)
RETURNS text
LANGUAGE sql
STRICT
AS $$
    SELECT regexp_replace(
        regexp_replace(
            regexp_replace(lower(unaccent(value)), '[^a-z0-9_-]+', '-', 'g'),
            '^[-_]+|[-_]+$',
            '',
            'g'
        ),
        '[-_]{2,}',
        '-',
        'g'
    );
$$;

-- The registry is the single foreign-key target for polymorphic ObjectRefs.
-- Concrete model tables remain authoritative for the actual records.
CREATE TABLE "objectRegistry" (
    id "objectRef" PRIMARY KEY
);

CREATE TABLE types (
    id            "typeRef" PRIMARY KEY,
    name          text NOT NULL,
    "wikipediaRef" text,
    locality      integer,
    stats         jsonb,
    "statsUpdated" timestamptz,
    knowledge     jsonb,
    "knowledgeUpdated" timestamptz,
    confidence    numeric,
    "confidenceUpdated" timestamptz,
    CHECK (id::text ~ '^syn://type/'),
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE things (
    id            "thingRef" PRIMARY KEY,
    name          text NOT NULL,
    "wikipediaRef" text,
    locality      integer,
    stats         jsonb,
    "statsUpdated" timestamptz,
    knowledge     jsonb,
    "knowledgeUpdated" timestamptz,
    confidence    numeric,
    "confidenceUpdated" timestamptz,
    CHECK (id::text ~ '^syn://thing/'),
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE events (
    id            "eventRef" PRIMARY KEY,
    name          text NOT NULL,
    locality      integer,
    "occurredAt"   timestamptz,
    stats         jsonb,
    "statsUpdated" timestamptz,
    knowledge     jsonb,
    "knowledgeUpdated" timestamptz,
    confidence    numeric,
    "confidenceUpdated" timestamptz,
    CHECK (id::text ~ '^syn://event/'),
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE embers (
    id            "emberRef" PRIMARY KEY,
    name          text NOT NULL,
    locality      integer,
    stats         jsonb,
    "statsUpdated" timestamptz,
    knowledge     jsonb,
    "knowledgeUpdated" timestamptz,
    confidence    numeric,
    "confidenceUpdated" timestamptz,
    CHECK (id::text ~ '^syn://ember/'),
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE cogs (
    id            "cogRef" PRIMARY KEY,
    name          text NOT NULL,
    locality      integer,
    stats         jsonb,
    "statsUpdated" timestamptz,
    knowledge     jsonb,
    "knowledgeUpdated" timestamptz,
    confidence    numeric,
    "confidenceUpdated" timestamptz,
    CHECK (id::text ~ '^syn://cog/'),
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE enigmas (
    id            "enigmaRef" PRIMARY KEY,
    name          text NOT NULL,
    locality      integer,
    stats         jsonb,
    "statsUpdated" timestamptz,
    knowledge     jsonb,
    "knowledgeUpdated" timestamptz,
    confidence    numeric,
    "confidenceUpdated" timestamptz,
    CHECK (id::text ~ '^syn://enigma/'),
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE pondles (
    id            "pondleRef" PRIMARY KEY,
    name          text NOT NULL,
    locality      integer,
    stats         jsonb,
    "statsUpdated" timestamptz,
    knowledge     jsonb,
    "knowledgeUpdated" timestamptz,
    confidence    numeric,
    "confidenceUpdated" timestamptz,
    CHECK (id::text ~ '^syn://pondle/'),
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE snugs (
    id            "snugRef" PRIMARY KEY,
    name          text NOT NULL,
    locality      integer,
    stats         jsonb,
    "statsUpdated" timestamptz,
    knowledge     jsonb,
    "knowledgeUpdated" timestamptz,
    confidence    numeric,
    "confidenceUpdated" timestamptz,
    CHECK (id::text ~ '^syn://snug/'),
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE oms (
    id            "omRef" PRIMARY KEY,
    name          text NOT NULL,
    locality      integer,
    stats         jsonb,
    "statsUpdated" timestamptz,
    knowledge     jsonb,
    "knowledgeUpdated" timestamptz,
    confidence    numeric,
    "confidenceUpdated" timestamptz,
    CHECK (id::text ~ '^syn://om/'),
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

-- Object identity is immutable because the contained slug is the primary key.
CREATE FUNCTION "preventObjectRefChange"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION
            'ObjectRef is immutable on %. Use an explicit reference migration to rename a slug',
            TG_TABLE_NAME
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER "typesPreventIdChange"
BEFORE UPDATE OF id ON types
FOR EACH ROW EXECUTE FUNCTION "preventObjectRefChange"();

CREATE TRIGGER "thingsPreventIdChange"
BEFORE UPDATE OF id ON things
FOR EACH ROW EXECUTE FUNCTION "preventObjectRefChange"();

CREATE TRIGGER "eventsPreventIdChange"
BEFORE UPDATE OF id ON events
FOR EACH ROW EXECUTE FUNCTION "preventObjectRefChange"();

CREATE TRIGGER "embersPreventIdChange"
BEFORE UPDATE OF id ON embers
FOR EACH ROW EXECUTE FUNCTION "preventObjectRefChange"();

CREATE TRIGGER "cogsPreventIdChange"
BEFORE UPDATE OF id ON cogs
FOR EACH ROW EXECUTE FUNCTION "preventObjectRefChange"();

CREATE TRIGGER "enigmasPreventIdChange"
BEFORE UPDATE OF id ON enigmas
FOR EACH ROW EXECUTE FUNCTION "preventObjectRefChange"();

CREATE TRIGGER "pondlesPreventIdChange"
BEFORE UPDATE OF id ON pondles
FOR EACH ROW EXECUTE FUNCTION "preventObjectRefChange"();

CREATE TRIGGER "snugsPreventIdChange"
BEFORE UPDATE OF id ON snugs
FOR EACH ROW EXECUTE FUNCTION "preventObjectRefChange"();

CREATE TRIGGER "omsPreventIdChange"
BEFORE UPDATE OF id ON oms
FOR EACH ROW EXECUTE FUNCTION "preventObjectRefChange"();

-- Model tables have no foreign key back to the registry. This AFTER trigger
-- inserts the registry row once the concrete row exists and removes it when the
-- concrete row is deleted.
CREATE FUNCTION "maintainObjectRegistry"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    "expectedTable" text := TG_ARGV[0];
    "suppliedId" "objectRef";
BEGIN
    "suppliedId" := CASE WHEN TG_OP = 'DELETE'
                        THEN OLD.id::"objectRef"
                        ELSE NEW.id::"objectRef"
                   END;

    IF "objectRefTable"("suppliedId") IS DISTINCT FROM "expectedTable" THEN
        RAISE EXCEPTION '%.id must contain "objectTable" %, received %',
            TG_TABLE_NAME, "expectedTable", "objectRefTable"("suppliedId")
            USING ERRCODE = 'check_violation';
    END IF;

    IF TG_OP = 'INSERT' THEN
        INSERT INTO "objectRegistry" (id)
        VALUES ("suppliedId")
        ON CONFLICT (id) DO NOTHING;
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        DELETE FROM "objectRegistry" WHERE id = "suppliedId";
        RETURN OLD;
    END IF;

    RAISE EXCEPTION 'unsupported registry trigger operation: %', TG_OP;
END;
$$;

CREATE TRIGGER "typesMaintainRegistry"
AFTER INSERT OR DELETE ON types
FOR EACH ROW EXECUTE FUNCTION "maintainObjectRegistry"('type');

CREATE TRIGGER "thingsMaintainRegistry"
AFTER INSERT OR DELETE ON things
FOR EACH ROW EXECUTE FUNCTION "maintainObjectRegistry"('thing');

CREATE TRIGGER "eventsMaintainRegistry"
AFTER INSERT OR DELETE ON events
FOR EACH ROW EXECUTE FUNCTION "maintainObjectRegistry"('event');

CREATE TRIGGER "embersMaintainRegistry"
AFTER INSERT OR DELETE ON embers
FOR EACH ROW EXECUTE FUNCTION "maintainObjectRegistry"('ember');

CREATE TRIGGER "cogsMaintainRegistry"
AFTER INSERT OR DELETE ON cogs
FOR EACH ROW EXECUTE FUNCTION "maintainObjectRegistry"('cog');

CREATE TRIGGER "enigmasMaintainRegistry"
AFTER INSERT OR DELETE ON enigmas
FOR EACH ROW EXECUTE FUNCTION "maintainObjectRegistry"('enigma');

CREATE TRIGGER "pondlesMaintainRegistry"
AFTER INSERT OR DELETE ON pondles
FOR EACH ROW EXECUTE FUNCTION "maintainObjectRegistry"('pondle');

CREATE TRIGGER "snugsMaintainRegistry"
AFTER INSERT OR DELETE ON snugs
FOR EACH ROW EXECUTE FUNCTION "maintainObjectRegistry"('snug');

CREATE TRIGGER "omsMaintainRegistry"
AFTER INSERT OR DELETE ON oms
FOR EACH ROW EXECUTE FUNCTION "maintainObjectRegistry"('om');

-- Direct registry changes could detach it from the concrete tables. Nested
-- writes made by the model triggers above are allowed.
CREATE FUNCTION "protectObjectRegistry"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION '"objectRegistry" is maintained by model-table triggers'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER "objectRegistryProtectRows"
BEFORE INSERT OR UPDATE OR DELETE ON "objectRegistry"
FOR EACH ROW EXECUTE FUNCTION "protectObjectRegistry"();

-- Conditional reference triggers replace ordinary foreign keys because only
-- syn:// references are local. Protect local registry rows from becoming
-- dangling references when their concrete records are deleted.
CREATE FUNCTION "preventReferencedLocalDelete"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM "objectTypes"
               WHERE "objectRef" = OLD.id OR "typeRef"::"objectRef" = OLD.id)
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
$$;

CREATE FUNCTION "preventRegistryBypass"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'TRUNCATE is disabled because it bypasses ObjectRef registry maintenance; use DELETE'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$;

CREATE TRIGGER "objectRegistryPreventTruncate"
BEFORE TRUNCATE ON "objectRegistry"
FOR EACH STATEMENT EXECUTE FUNCTION "preventRegistryBypass"();

CREATE TRIGGER "typesPreventTruncate"
BEFORE TRUNCATE ON types
FOR EACH STATEMENT EXECUTE FUNCTION "preventRegistryBypass"();

CREATE TRIGGER "thingsPreventTruncate"
BEFORE TRUNCATE ON things
FOR EACH STATEMENT EXECUTE FUNCTION "preventRegistryBypass"();

CREATE TRIGGER "eventsPreventTruncate"
BEFORE TRUNCATE ON events
FOR EACH STATEMENT EXECUTE FUNCTION "preventRegistryBypass"();

CREATE TRIGGER "embersPreventTruncate"
BEFORE TRUNCATE ON embers
FOR EACH STATEMENT EXECUTE FUNCTION "preventRegistryBypass"();

CREATE TRIGGER "cogsPreventTruncate"
BEFORE TRUNCATE ON cogs
FOR EACH STATEMENT EXECUTE FUNCTION "preventRegistryBypass"();

CREATE TRIGGER "enigmasPreventTruncate"
BEFORE TRUNCATE ON enigmas
FOR EACH STATEMENT EXECUTE FUNCTION "preventRegistryBypass"();

CREATE TRIGGER "pondlesPreventTruncate"
BEFORE TRUNCATE ON pondles
FOR EACH STATEMENT EXECUTE FUNCTION "preventRegistryBypass"();

CREATE TRIGGER "snugsPreventTruncate"
BEFORE TRUNCATE ON snugs
FOR EACH STATEMENT EXECUTE FUNCTION "preventRegistryBypass"();

CREATE TRIGGER "omsPreventTruncate"
BEFORE TRUNCATE ON oms
FOR EACH STATEMENT EXECUTE FUNCTION "preventRegistryBypass"();

CREATE TABLE "objectTypes" (
    "objectRef" "objectRef" NOT NULL,
    "typeRef" "typeRef" NOT NULL,
    PRIMARY KEY ("objectRef", "typeRef"),
    CHECK ("objectRefTable"("typeRef"::"objectRef") = 'type')
);

CREATE TABLE "eventTargets" (
    "eventRef" "eventRef" NOT NULL,
    "targetRef" "objectRef" NOT NULL,
    role slug,
    position integer CHECK (position IS NULL OR position >= 0),
    PRIMARY KEY ("eventRef", "targetRef"),
    CHECK ("objectRefTable"("eventRef"::"objectRef") = 'event')
);

CREATE TABLE "objectRelationships" (
    "sourceRef" "objectRef" NOT NULL,
    "targetRef" "objectRef" NOT NULL,
    "relationshipType" slug NOT NULL,
    properties jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY ("sourceRef", "targetRef", "relationshipType")
);

-- The following trigger verifies the registry kind independently of a domain.
-- It is reusable for any future typed ObjectRef column.
CREATE FUNCTION "requireRegistryKind"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    "refColumn" text := TG_ARGV[0];
    "requiredKind" text := TG_ARGV[1];
    "suppliedRef" "objectRef";
BEGIN
    "suppliedRef" := (to_jsonb(NEW) ->> "refColumn")::"objectRef";

    IF "objectRefTable"("suppliedRef") <> "requiredKind" THEN
        RAISE EXCEPTION '% must be a local % reference; received %',
            "refColumn", "requiredKind", "suppliedRef"
            USING ERRCODE = 'check_violation';
    END IF;

    PERFORM "ensureLocalObject"("suppliedRef");

    RETURN NEW;
END;
$$;

-- Materialize a required-field-only placeholder whenever a valid local
-- ObjectRef is used before its concrete record exists. Inserts are idempotent
-- and concurrency-safe; each model's existing AFTER INSERT trigger registers
-- the new object.
CREATE FUNCTION "ensureLocalObject"("candidateRef" "objectRef")
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
            INSERT INTO things (id, name)
            VALUES ("candidateRef"::"thingRef", "objectSlug"::text)
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
$$;

CREATE FUNCTION "requireLocalObjectIfSyn"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    "refColumn" text := TG_ARGV[0];
    "suppliedRef" "objectRef";
BEGIN
    "suppliedRef" := (to_jsonb(NEW) ->> "refColumn")::"objectRef";

    IF "suppliedRef"::text ~ '^syn://' THEN
        PERFORM "ensureLocalObject"("suppliedRef");
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER "objectTypesRequireObjectIfLocal"
BEFORE INSERT OR UPDATE OF "objectRef" ON "objectTypes"
FOR EACH ROW EXECUTE FUNCTION "requireLocalObjectIfSyn"('objectRef');

CREATE TRIGGER "objectTypesRequireType"
BEFORE INSERT OR UPDATE OF "typeRef" ON "objectTypes"
FOR EACH ROW EXECUTE FUNCTION "requireRegistryKind"('typeRef', 'type');

CREATE TRIGGER "eventTargetsRequireTargetIfLocal"
BEFORE INSERT OR UPDATE OF "targetRef" ON "eventTargets"
FOR EACH ROW EXECUTE FUNCTION "requireLocalObjectIfSyn"('targetRef');

CREATE TRIGGER "eventTargetsRequireEvent"
BEFORE INSERT OR UPDATE OF "eventRef" ON "eventTargets"
FOR EACH ROW EXECUTE FUNCTION "requireRegistryKind"('eventRef', 'event');

CREATE TRIGGER "objectRelationshipsRequireSourceIfLocal"
BEFORE INSERT OR UPDATE OF "sourceRef" ON "objectRelationships"
FOR EACH ROW EXECUTE FUNCTION "requireLocalObjectIfSyn"('sourceRef');

CREATE TRIGGER "objectRelationshipsRequireTargetIfLocal"
BEFORE INSERT OR UPDATE OF "targetRef" ON "objectRelationships"
FOR EACH ROW EXECUTE FUNCTION "requireLocalObjectIfSyn"('targetRef');

CREATE TYPE "chronicleKind" AS ENUM ('ledger', 'provenance');

CREATE TABLE "objectChronicle" (
    "entryId" bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    "objectRef" "objectRef" NOT NULL,
    "eventRef" "eventRef",
    kind "chronicleKind" NOT NULL,
    "begunAt" timestamptz NOT NULL,
    "endedAt" timestamptz,
    "sequenceNumber" bigint NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE ("objectRef", kind, "sequenceNumber"),
    CHECK ("endedAt" IS NULL OR "endedAt" >= "begunAt"),
    CHECK (
        "eventRef" IS NULL
        OR "objectRefTable"("eventRef"::"objectRef") = 'event'
    )
);

CREATE TRIGGER "objectRegistryPreventReferencedDelete"
BEFORE DELETE ON "objectRegistry"
FOR EACH ROW EXECUTE FUNCTION "preventReferencedLocalDelete"();

CREATE TRIGGER "objectChronicleRequireObjectIfLocal"
BEFORE INSERT OR UPDATE OF "objectRef" ON "objectChronicle"
FOR EACH ROW EXECUTE FUNCTION "requireLocalObjectIfSyn"('objectRef');

CREATE TRIGGER "objectChronicleRequireEvent"
BEFORE INSERT OR UPDATE OF "eventRef" ON "objectChronicle"
FOR EACH ROW
WHEN (NEW."eventRef" IS NOT NULL)
EXECUTE FUNCTION "requireRegistryKind"('eventRef', 'event');

-- Use this validator in triggers on future "observationRef" and "balanceRef"
-- columns. Cross-table classification membership cannot safely be a domain
-- CHECK constraint.
CREATE FUNCTION "assertObjectHasType"(
    "candidateRef" "objectRef",
    "requiredTypeSlug" slug
)
RETURNS void
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    "requiredTypeRef" "typeRef" :=
        "makeObjectRef"('type', "requiredTypeSlug")::"typeRef";
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM "objectTypes" ot
        WHERE ot."objectRef" = "candidateRef"
          AND ot."typeRef" = "requiredTypeRef"
    ) THEN
        RAISE EXCEPTION '% is not classified as %',
            "candidateRef", "requiredTypeSlug"
            USING ERRCODE = 'check_violation';
    END IF;
END;
$$;

CREATE INDEX "objectTypesTypeIdx"
    ON "objectTypes" ("typeRef", "objectRef");

CREATE INDEX "eventTargetsTargetIdx"
    ON "eventTargets" ("targetRef", "eventRef");

CREATE INDEX "objectRelationshipsTargetIdx"
    ON "objectRelationships" ("targetRef", "relationshipType");

CREATE INDEX "objectChronicleTimelineIdx"
    ON "objectChronicle" ("objectRef", kind, "begunAt" DESC);

CREATE INDEX "typesStatsGinIdx" ON types USING gin (stats);
CREATE INDEX "thingsStatsGinIdx" ON things USING gin (stats);
CREATE INDEX "eventsStatsGinIdx" ON events USING gin (stats);
CREATE INDEX "embersStatsGinIdx" ON embers USING gin (stats);
CREATE INDEX "cogsStatsGinIdx" ON cogs USING gin (stats);
CREATE INDEX "enigmasStatsGinIdx" ON enigmas USING gin (stats);
CREATE INDEX "pondlesStatsGinIdx" ON pondles USING gin (stats);
CREATE INDEX "snugsStatsGinIdx" ON snugs USING gin (stats);
CREATE INDEX "omsStatsGinIdx" ON oms USING gin (stats);

COMMIT;

-- Examples:
-- INSERT INTO types (id, name)
-- VALUES ("makeObjectRef"('type', 'observation'), 'Observation');
--
-- INSERT INTO events (id, name)
-- VALUES ("makeObjectRef"('event', 'sensor-reading-001'), 'Sensor reading');
--
-- SELECT "objectRefTable"(id::"objectRef"),
--        "objectRefSlug"(id::"objectRef"),
--        name
-- FROM events;

