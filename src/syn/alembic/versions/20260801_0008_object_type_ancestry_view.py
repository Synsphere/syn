"""Rename the inherited object type view after retiring objectTypes."""

from alembic import op


revision = "20260801_0008"
down_revision = "20260801_0007"
branch_labels = None
depends_on = None


OBJECT_HAS_TYPE = r'''
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
        FROM "objectTypesInherited"
        WHERE "objectRef" = "candidateRef"
          AND "typeRef" = "requiredTypeRef"
    );
$$
'''


def upgrade() -> None:
    op.execute('ALTER VIEW "objectTypesInherited" RENAME TO "objectTypeAncestry"')
    op.execute(OBJECT_HAS_TYPE)


def downgrade() -> None:
    op.execute('ALTER VIEW "objectTypeAncestry" RENAME TO "objectTypesInherited"')
    op.execute(OBJECT_HAS_TYPE_DOWNGRADE)
