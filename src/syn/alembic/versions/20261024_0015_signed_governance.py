"""Append-only signed genesis and locally governed policy successors."""

from alembic import op
import sqlalchemy as sa

revision = "20261024_0015"
down_revision = "20261023_0014"
branch_labels = None
depends_on = None

TABLES = ("syn_governed_catalogs", "syn_governed_records", "syn_governed_namespace_claims")


def upgrade() -> None:
    op.create_table(
        TABLES[0],
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("syn_policy_workspaces.workspace_id"),
            primary_key=True,
        ),
        sa.Column("catalog_id", sa.String(36), primary_key=True),
        sa.Column("genesis_record_digest", sa.String(64), nullable=False),
    )
    op.create_table(
        TABLES[1],
        sa.Column("workspace_id", sa.String(36), primary_key=True),
        sa.Column("catalog_id", sa.String(36), primary_key=True),
        sa.Column("sequence", sa.BigInteger(), primary_key=True),
        sa.Column("record_digest", sa.String(64), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "catalog_id"], [TABLES[0] + ".workspace_id", TABLES[0] + ".catalog_id"]
        ),
        sa.UniqueConstraint("workspace_id", "catalog_id", "record_digest"),
    )
    op.create_table(
        TABLES[2],
        sa.Column("workspace_id", sa.String(36), primary_key=True),
        sa.Column("domain_id", sa.String(36), primary_key=True),
        sa.Column("conflict_namespace", sa.String(128), primary_key=True),
        sa.Column("catalog_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "catalog_id"], [TABLES[0] + ".workspace_id", TABLES[0] + ".catalog_id"]
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "domain_id"],
            ["syn_policy_domains.workspace_id", "syn_policy_domains.domain_id"],
        ),
    )
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER syn_policy_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION prevent_syn_policy_rewrite()"
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
