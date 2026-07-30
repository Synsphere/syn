"""Add the polyhierarchical Syn type graph and seed its core ontology."""

from alembic import op

from syn.ontology import ontologySqlStatements


revision = "20260729_0002"
down_revision = "20260729_0001"
branch_labels = None
depends_on = None


CREATE_TYPE_GRAPH_STATEMENTS = (
    """
    CREATE TABLE "typeParents" (
        "childRef" "typeRef" NOT NULL,
        "parentRef" "typeRef" NOT NULL,
        PRIMARY KEY ("childRef", "parentRef"),
        CONSTRAINT "typeParentsDifferentCheck"
            CHECK ("childRef" <> "parentRef")
    )
    """,
    """
    CREATE INDEX "typeParentsParentIdx"
        ON "typeParents" ("parentRef", "childRef")
    """,
    """
    CREATE FUNCTION "preventTypeParentChange"()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        RAISE EXCEPTION
            'type parent edges are immutable; delete and insert the edge instead'
            USING ERRCODE = 'integrity_constraint_violation';
    END;
    $$
    """,
    """
    CREATE FUNCTION "validateTypeParent"()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        -- Serializing edge validation prevents two concurrent transactions from
        -- introducing opposite edges after both observe an acyclic graph.
        PERFORM pg_advisory_xact_lock(hashtextextended('syn.typeParents', 0));

        IF NEW."childRef" = NEW."parentRef" THEN
            RAISE EXCEPTION 'a type cannot be its own parent: %', NEW."childRef"
                USING ERRCODE = 'check_violation';
        END IF;

        IF EXISTS (
            WITH RECURSIVE ancestors("typeRef") AS (
                SELECT "parentRef"
                FROM "typeParents"
                WHERE "childRef" = NEW."parentRef"
                UNION
                SELECT edge."parentRef"
                FROM "typeParents" edge
                JOIN ancestors
                  ON edge."childRef" = ancestors."typeRef"
            )
            SELECT 1
            FROM ancestors
            WHERE "typeRef" = NEW."childRef"
        ) THEN
            RAISE EXCEPTION 'type parent edge % -> % would create a cycle',
                NEW."childRef", NEW."parentRef"
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

        RETURN NEW;
    END;
    $$
    """,
    """
    CREATE TRIGGER "typeParents05PreventEdgeChange"
    BEFORE UPDATE OF "childRef", "parentRef" ON "typeParents"
    FOR EACH ROW EXECUTE FUNCTION "preventTypeParentChange"()
    """,
    """
    CREATE TRIGGER "typeParents10RequireChild"
    BEFORE INSERT OR UPDATE OF "childRef" ON "typeParents"
    FOR EACH ROW EXECUTE FUNCTION "requireRegistryKind"('childRef', 'type')
    """,
    """
    CREATE TRIGGER "typeParents20RequireParent"
    BEFORE INSERT OR UPDATE OF "parentRef" ON "typeParents"
    FOR EACH ROW EXECUTE FUNCTION "requireRegistryKind"('parentRef', 'type')
    """,
    """
    CREATE TRIGGER "typeParents30Validate"
    BEFORE INSERT ON "typeParents"
    FOR EACH ROW EXECUTE FUNCTION "validateTypeParent"()
    """,
    """
    CREATE TRIGGER "typeParentsPreventTruncate"
    BEFORE TRUNCATE ON "typeParents"
    FOR EACH STATEMENT EXECUTE FUNCTION "preventRegistryBypass"()
    """,
    """
    CREATE OR REPLACE FUNCTION "preventReferencedLocalDelete"()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF EXISTS (SELECT 1 FROM "objectTypes"
                   WHERE "objectRef" = OLD.id OR "typeRef"::"objectRef" = OLD.id)
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
    """,
    """
    CREATE VIEW "typeAncestry" AS
    WITH RECURSIVE ancestry("descendantRef", "ancestorRef", depth) AS (
        SELECT "childRef", "parentRef", 1
        FROM "typeParents"
        UNION ALL
        SELECT ancestry."descendantRef", edge."parentRef", ancestry.depth + 1
        FROM ancestry
        JOIN "typeParents" edge
          ON edge."childRef" = ancestry."ancestorRef"
    )
    SELECT "descendantRef", "ancestorRef", min(depth)::integer AS depth
    FROM ancestry
    GROUP BY "descendantRef", "ancestorRef"
    """,
    """
    CREATE VIEW "objectTypesInherited" AS
    SELECT "objectRef", "typeRef", 0::integer AS depth
    FROM "objectTypes"
    UNION
    SELECT direct."objectRef", ancestry."ancestorRef" AS "typeRef", ancestry.depth
    FROM "objectTypes" direct
    JOIN "typeAncestry" ancestry
      ON ancestry."descendantRef" = direct."typeRef"
    """,
    """
    CREATE FUNCTION "objectHasType"(
        "candidateRef" "objectRef",
        "requiredTypeRef" "typeRef"
    )
    RETURNS boolean
    LANGUAGE sql
    STABLE
    AS $$
        SELECT EXISTS (
            SELECT 1
            FROM "objectTypesInherited"
            WHERE "objectRef" = "candidateRef"
              AND "typeRef" = "requiredTypeRef"
        );
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION "assertObjectHasType"(
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
        IF NOT "objectHasType"("candidateRef", "requiredTypeRef") THEN
            RAISE EXCEPTION '% is not classified as %',
                "candidateRef", "requiredTypeSlug"
                USING ERRCODE = 'check_violation';
        END IF;
    END;
    $$
    """,
)


def upgrade() -> None:
    for statement in CREATE_TYPE_GRAPH_STATEMENTS:
        op.execute(statement)
    for statement in ontologySqlStatements():
        op.execute(statement)


def downgrade() -> None:
    statements = (
        """
        CREATE OR REPLACE FUNCTION "assertObjectHasType"(
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
                FROM "objectTypes"
                WHERE "objectRef" = "candidateRef"
                  AND "typeRef" = "requiredTypeRef"
            ) THEN
                RAISE EXCEPTION '% is not classified as %',
                    "candidateRef", "requiredTypeSlug"
                    USING ERRCODE = 'check_violation';
            END IF;
        END;
        $$
        """,
        'DROP FUNCTION IF EXISTS "objectHasType"("objectRef", "typeRef")',
        'DROP VIEW IF EXISTS "objectTypesInherited"',
        'DROP VIEW IF EXISTS "typeAncestry"',
        """
        CREATE OR REPLACE FUNCTION "preventReferencedLocalDelete"()
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
        $$
        """,
        'DROP TABLE IF EXISTS "typeParents" CASCADE',
        'DROP FUNCTION IF EXISTS "validateTypeParent"()',
        'DROP FUNCTION IF EXISTS "preventTypeParentChange"()',
    )
    for statement in statements:
        op.execute(statement)

    # Seeded type records are intentionally retained. They may have acquired
    # objectTypes references or user-authored knowledge after installation.
