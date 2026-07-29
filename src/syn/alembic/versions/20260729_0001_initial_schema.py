"""Create the initial Syn object-reference schema."""

from alembic import op

from syn.schema import schemaStatements


revision = "20260729_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in schemaStatements():
        op.execute(statement)


def downgrade() -> None:
    statements = (
        'DROP TABLE IF EXISTS "objectChronicle" CASCADE',
        'DROP TABLE IF EXISTS "objectRelationships" CASCADE',
        'DROP TABLE IF EXISTS "eventTargets" CASCADE',
        'DROP TABLE IF EXISTS "objectTypes" CASCADE',
        'DROP TABLE IF EXISTS oms CASCADE',
        'DROP TABLE IF EXISTS snugs CASCADE',
        'DROP TABLE IF EXISTS pondles CASCADE',
        'DROP TABLE IF EXISTS enigmas CASCADE',
        'DROP TABLE IF EXISTS cogs CASCADE',
        'DROP TABLE IF EXISTS embers CASCADE',
        'DROP TABLE IF EXISTS events CASCADE',
        'DROP TABLE IF EXISTS things CASCADE',
        'DROP TABLE IF EXISTS types CASCADE',
        'DROP TABLE IF EXISTS "objectRegistry" CASCADE',
        'DROP FUNCTION IF EXISTS "assertObjectHasType"("objectRef", slug)',
        'DROP FUNCTION IF EXISTS "requireLocalObjectIfSyn"()',
        'DROP FUNCTION IF EXISTS "ensureLocalObject"("objectRef")',
        'DROP FUNCTION IF EXISTS "requireRegistryKind"()',
        'DROP FUNCTION IF EXISTS "preventRegistryBypass"()',
        'DROP FUNCTION IF EXISTS "preventReferencedLocalDelete"()',
        'DROP FUNCTION IF EXISTS "protectObjectRegistry"()',
        'DROP FUNCTION IF EXISTS "maintainObjectRegistry"()',
        'DROP FUNCTION IF EXISTS "preventObjectRefChange"()',
        'DROP FUNCTION IF EXISTS "normalizeSlug"(text)',
        'DROP FUNCTION IF EXISTS "objectRefSlug"("objectRef")',
        'DROP FUNCTION IF EXISTS "objectRefTable"("objectRef")',
        'DROP FUNCTION IF EXISTS "makeObjectRef"(text, slug)',
        'DROP TYPE IF EXISTS "chronicleKind"',
        'DROP DOMAIN IF EXISTS "balanceRef"',
        'DROP DOMAIN IF EXISTS "observationRef"',
        'DROP DOMAIN IF EXISTS "omRef"',
        'DROP DOMAIN IF EXISTS "snugRef"',
        'DROP DOMAIN IF EXISTS "pondleRef"',
        'DROP DOMAIN IF EXISTS "enigmaRef"',
        'DROP DOMAIN IF EXISTS "cogRef"',
        'DROP DOMAIN IF EXISTS "emberRef"',
        'DROP DOMAIN IF EXISTS "eventRef"',
        'DROP DOMAIN IF EXISTS "thingRef"',
        'DROP DOMAIN IF EXISTS "typeRef"',
        'DROP DOMAIN IF EXISTS "objectRef"',
        'DROP DOMAIN IF EXISTS slug',
    )
    for statement in statements:
        op.execute(statement)
