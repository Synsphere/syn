"""Add the Syn feed-type ontology."""

from alembic import op

from syn.feeds import FEED_TYPE_REFS, feedTypeSqlStatements


revision = "20260730_0003"
down_revision = "20260729_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in feedTypeSqlStatements():
        op.execute(statement)


def downgrade() -> None:
    # Remove only feed-type edges. Type records are retained because they may
    # have acquired user knowledge or object classifications after installation.
    refs = ", ".join("'" + value.replace("'", "''") + "'" for value in FEED_TYPE_REFS)
    op.execute(
        'DELETE FROM "typeParents" '
        f'WHERE "childRef"::text IN ({refs})'
    )
