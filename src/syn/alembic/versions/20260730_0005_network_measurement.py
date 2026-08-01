"""Add the network-measurement ontology type."""

from alembic import op

from syn.ontology import ontologySqlStatements


revision = "20260730_0005"
down_revision = "20260730_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in ontologySqlStatements():
        op.execute(statement)


def downgrade() -> None:
    op.execute(
        '''
        DELETE FROM "typeParents"
        WHERE "childRef" = 'syn://type/network-measurement'::"typeRef"
        '''
    )
    # Retain the type record because application objects may reference it.
