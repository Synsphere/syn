"""Public peer checkpoint archive and immutable node-ledger witness receipts.

Freeze schema here; never import mutable runtime metadata into a migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261117_0020"
down_revision = "20261117_0019"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("operatorDagWitnessHeads",
        sa.Column("observerHash", sa.String(64), primary_key=True),
        sa.Column("peerHash", sa.String(64), primary_key=True),
        sa.Column("networkId", sa.String(64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("lastRecord", sa.String(64)),
        sa.Column("checkpoint", sa.Text()),
        sa.Column("lastOutcome", sa.String(32), nullable=False),
        sa.Column("conflicted", sa.Boolean(), nullable=False),
        sa.Column("checkedAt", sa.DateTime(timezone=True), nullable=False))
    op.create_table("operatorDagWitnessRecords",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("observerHash", sa.String(64), nullable=False),
        sa.Column("peerHash", sa.String(64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("receiptSequence", sa.BigInteger(), nullable=False),
        sa.Column("receiptHash", sa.String(64), nullable=False),
        sa.UniqueConstraint("observerHash", "peerHash", "sequence", name="operatorDagWitnessSequenceUnique"))
    op.execute('''CREATE TRIGGER "operatorDagWitnessImmutableRows" BEFORE UPDATE OR DELETE
        ON "operatorDagWitnessRecords" FOR EACH ROW EXECUTE FUNCTION "synOperatorDagImmutable"()''')
    op.execute('''CREATE TRIGGER "operatorDagWitnessImmutableTruncate" BEFORE TRUNCATE
        ON "operatorDagWitnessRecords" FOR EACH STATEMENT EXECUTE FUNCTION "synOperatorDagImmutable"()''')


def downgrade():
    raise RuntimeError("Witness evidence requires reviewed archival, not a destructive downgrade")
