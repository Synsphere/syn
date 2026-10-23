"""Durable workspace communication and append-only hash-chain receipts."""

from alembic import op
import sqlalchemy as sa

revision = "20261023_0012"
down_revision = "20260821_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "syn_communication_heads",
        sa.Column("workspace_id", sa.String(36), primary_key=True),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.CheckConstraint("sequence >= 0", name="syn_communication_head_sequence"),
    )
    op.create_table(
        "syn_communication_messages",
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("syn_communication_heads.workspace_id"),
            primary_key=True,
        ),
        sa.Column("sequence", sa.BigInteger(), primary_key=True),
        sa.Column("message_id", sa.String(36), nullable=False),
        sa.Column("channel", sa.String(128), nullable=False),
        sa.Column("sender", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.String(40), nullable=False),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "message_id", name="syn_communication_message_id"
        ),
        sa.CheckConstraint("sequence > 0", name="syn_communication_message_sequence"),
    )
    op.execute("""
        CREATE FUNCTION "synCommunicationImmutable"() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Syn communication messages are append-only';
        END; $$;
        CREATE TRIGGER "synCommunicationImmutableRows"
            BEFORE UPDATE OR DELETE ON syn_communication_messages
            FOR EACH ROW EXECUTE FUNCTION "synCommunicationImmutable"();
        CREATE TRIGGER "synCommunicationImmutableTruncate"
            BEFORE TRUNCATE ON syn_communication_messages
            FOR EACH STATEMENT EXECUTE FUNCTION "synCommunicationImmutable"();
    """)


def downgrade() -> None:
    op.drop_table("syn_communication_messages")
    op.drop_table("syn_communication_heads")
    op.execute('DROP FUNCTION "synCommunicationImmutable"()')
