"""Add property declarations for types."""

from alembic import op


revision = "20260801_0009"
down_revision = "20260801_0008"
branch_labels = None
depends_on = None


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
    '''       OR EXISTS (SELECT 1 FROM "typeProperties"
                  WHERE "type"::"objectRef" = OLD.id
                     OR "propertyTypeRef"::"objectRef" = OLD.id)
''',
    "",
)


def upgrade() -> None:
    statements = (
        r'''
        CREATE TABLE "typeProperties" (
            "type" "typeRef" NOT NULL,
            name varchar(1024) NOT NULL,
            "propertyTypeRef" "typeRef" NOT NULL,
            min bigint NOT NULL DEFAULT 1,
            max bigint NOT NULL DEFAULT 1,
            PRIMARY KEY ("type", name),
            CONSTRAINT "typePropertiesMinCheck" CHECK (min >= 0),
            CONSTRAINT "typePropertiesRangeCheck" CHECK (max >= min)
        )
        ''',
        r'''
        CREATE INDEX "typePropertiesPropertyTypeRefIdx"
        ON "typeProperties" ("propertyTypeRef", "type", name)
        ''',
        r'''
        CREATE TRIGGER "typePropertiesRequireType"
        BEFORE INSERT OR UPDATE OF "type" ON "typeProperties"
        FOR EACH ROW EXECUTE FUNCTION "requireRegistryKind"('type', 'type')
        ''',
        r'''
        CREATE TRIGGER "typePropertiesRequirePropertyType"
        BEFORE INSERT OR UPDATE OF "propertyTypeRef" ON "typeProperties"
        FOR EACH ROW EXECUTE FUNCTION "requireRegistryKind"('propertyTypeRef', 'type')
        ''',
        PREVENT_REFERENCED_DELETE,
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    op.execute(PREVENT_REFERENCED_DELETE_DOWNGRADE)
    op.execute('DROP TABLE "typeProperties"')
