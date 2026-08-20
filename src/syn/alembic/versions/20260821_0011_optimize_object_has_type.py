"""Avoid materializing all object ancestry for one membership lookup."""

from alembic import op


revision = "20260821_0011"
down_revision = "20260811_0010"
branch_labels = None
depends_on = None


OBJECT_HAS_TYPE = r'''
CREATE OR REPLACE FUNCTION "objectHasType"(
    "candidateRef" "objectRef",
    "requiredTypeRef" "typeRef"
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    "directTypeRef" "typeRef";
BEGIN
    CASE "objectRefTable"("candidateRef")
        WHEN 'thing' THEN
            SELECT "typeRef" INTO "directTypeRef"
            FROM things WHERE id = "candidateRef"::"thingRef";
        WHEN 'event' THEN
            SELECT "typeRef" INTO "directTypeRef"
            FROM events WHERE id = "candidateRef"::"eventRef";
        WHEN 'ember' THEN
            SELECT "typeRef" INTO "directTypeRef"
            FROM embers WHERE id = "candidateRef"::"emberRef";
        WHEN 'cog' THEN
            SELECT "typeRef" INTO "directTypeRef"
            FROM cogs WHERE id = "candidateRef"::"cogRef";
        WHEN 'credential' THEN
            SELECT "typeRef" INTO "directTypeRef"
            FROM credentials WHERE id = "candidateRef"::"credentialRef";
        WHEN 'enigma' THEN
            SELECT "typeRef" INTO "directTypeRef"
            FROM enigmas WHERE id = "candidateRef"::"enigmaRef";
        WHEN 'pondle' THEN
            SELECT "typeRef" INTO "directTypeRef"
            FROM pondles WHERE id = "candidateRef"::"pondleRef";
        WHEN 'snug' THEN
            SELECT "typeRef" INTO "directTypeRef"
            FROM snugs WHERE id = "candidateRef"::"snugRef";
        WHEN 'om' THEN
            SELECT "typeRef" INTO "directTypeRef"
            FROM oms WHERE id = "candidateRef"::"omRef";
        ELSE
            RETURN false;
    END CASE;

    IF "directTypeRef" IS NULL THEN
        RETURN false;
    END IF;

    RETURN "directTypeRef" = "requiredTypeRef"
        OR EXISTS (
            SELECT 1
            FROM "typeAncestry"
            WHERE "descendantRef" = "directTypeRef"
              AND "ancestorRef" = "requiredTypeRef"
        );
END;
$$
'''


OBJECT_HAS_TYPE_DOWNGRADE = r'''
CREATE OR REPLACE FUNCTION "objectHasType"(
    "candidateRef" "objectRef",
    "requiredTypeRef" "typeRef"
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM "objectTypeAncestry"
        WHERE "objectRef" = "candidateRef"
          AND "typeRef" = "requiredTypeRef"
    );
$$
'''


def upgrade() -> None:
    op.execute(OBJECT_HAS_TYPE)


def downgrade() -> None:
    op.execute(OBJECT_HAS_TYPE_DOWNGRADE)
