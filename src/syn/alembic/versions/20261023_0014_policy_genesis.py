"""Durable local genesis anchors and cross-catalog namespace ownership."""

from alembic import op
import sqlalchemy as sa

revision = "20261023_0014"
down_revision = "20261023_0013"
branch_labels = None
depends_on = None

TABLES = (
    "syn_policy_workspaces",
    "syn_policy_adoptions",
    "syn_policy_domains",
    "syn_policy_conflict_owners",
)


def upgrade() -> None:
    op.create_table(TABLES[0], sa.Column("workspace_id", sa.String(36), primary_key=True))
    op.create_table(
        TABLES[1],
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("syn_policy_workspaces.workspace_id"),
            primary_key=True,
        ),
        sa.Column("catalog_id", sa.String(36), primary_key=True),
        sa.Column("adoption_digest", sa.String(64), nullable=False),
        sa.Column("release_digest", sa.String(64), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
    )
    op.create_table(
        TABLES[2],
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("syn_policy_workspaces.workspace_id"),
            primary_key=True,
        ),
        sa.Column("domain_id", sa.String(36), primary_key=True),
        sa.Column("genesis_digest", sa.String(64), nullable=False),
    )
    op.create_table(
        TABLES[3],
        sa.Column("workspace_id", sa.String(36), primary_key=True),
        sa.Column("domain_id", sa.String(36), primary_key=True),
        sa.Column("conflict_namespace", sa.String(128), primary_key=True),
        sa.Column("catalog_id", sa.String(36), nullable=False),
        sa.Column("policy_digest", sa.String(64), nullable=False),
        sa.Column("stream_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "domain_id"],
            ["syn_policy_domains.workspace_id", "syn_policy_domains.domain_id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "catalog_id"],
            ["syn_policy_adoptions.workspace_id", "syn_policy_adoptions.catalog_id"],
        ),
    )
    op.execute("""
        CREATE FUNCTION prevent_syn_policy_rewrite() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Policy genesis records are immutable; governed transitions required'
                USING ERRCODE = '23000';
        END $$
    """)
    for table in TABLES:
        op.execute(f"""
            CREATE TRIGGER syn_policy_immutable
            BEFORE UPDATE OR DELETE OR TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION prevent_syn_policy_rewrite()
        """)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
    op.execute("DROP FUNCTION prevent_syn_policy_rewrite()")
