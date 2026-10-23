"""First-class draft consensus policy on canonical Type authoring records."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261023_0013"
down_revision = "20261023_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("types", sa.Column("consensusPolicy", JSONB(), nullable=True))
    op.create_check_constraint(
        "typesConsensusPolicyObject", "types",
        "\"consensusPolicy\" IS NULL OR jsonb_typeof(\"consensusPolicy\") = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint("typesConsensusPolicyObject", "types", type_="check")
    op.drop_column("types", "consensusPolicy")
