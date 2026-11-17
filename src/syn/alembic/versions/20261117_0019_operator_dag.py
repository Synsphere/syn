"""Signed Operator journal DAG projection (append-only nodes, updateable head)."""
from alembic import op
import sqlalchemy as sa

revision = "20261117_0019"
down_revision = "20261117_0018"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("operatorDagNodes",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("operatorHash", sa.String(64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("sourceIndex", sa.BigInteger(), nullable=False),
        sa.Column("record", sa.Text(), nullable=False),
        sa.UniqueConstraint("operatorHash", "sequence", name="operatorDagNodeSequenceUnique"),
        sa.UniqueConstraint("operatorHash", "source", "sourceIndex", name="operatorDagSourceUnique"))
    op.create_table("operatorDagCheckpoints",
        sa.Column("operatorHash", sa.String(64), primary_key=True),
        sa.Column("sequence", sa.BigInteger(), primary_key=True),
        sa.Column("root", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(128), nullable=False),
        sa.Column("publicKey", sa.String(64), nullable=False),
        sa.Column("networkId", sa.String(64), nullable=False),
        sa.Column("body", sa.Text(), nullable=False))
    # Guard immutable leaf records from ordinary UPDATE/DELETE/TRUNCATE operations.
    # Privileged DB owners can drop these guards; externally retained checkpoints
    # are needed for protection against wholesale database replacement.
    op.execute('''CREATE FUNCTION "synOperatorDagImmutable"() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Operator DAG leaf history is immutable'; END $$''')
    op.execute('''CREATE TRIGGER "synOperatorDagImmutableRows" BEFORE UPDATE OR DELETE
        ON "operatorDagNodes" FOR EACH ROW EXECUTE FUNCTION "synOperatorDagImmutable"()''')
    op.execute('''CREATE TRIGGER "synOperatorDagImmutableTruncate" BEFORE TRUNCATE
        ON "operatorDagNodes" FOR EACH STATEMENT EXECUTE FUNCTION "synOperatorDagImmutable"()''')
    op.execute('''CREATE TRIGGER "synOperatorDagCheckpointsImmutableRows" BEFORE UPDATE OR DELETE
        ON "operatorDagCheckpoints" FOR EACH ROW EXECUTE FUNCTION "synOperatorDagImmutable"()''')
    op.execute('''CREATE TRIGGER "synOperatorDagCheckpointsImmutableTruncate" BEFORE TRUNCATE
        ON "operatorDagCheckpoints" FOR EACH STATEMENT EXECUTE FUNCTION "synOperatorDagImmutable"()''')


def downgrade():
    # Never silently erase authenticated Operator history.
    raise RuntimeError("Operator DAG downgrade requires an explicit reviewed archival migration")
