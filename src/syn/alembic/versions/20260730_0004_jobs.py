"""Add persistent process-scheduler jobs."""

from alembic import op


revision = "20260730_0004"
down_revision = "20260730_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE \"jobStatus\" AS ENUM "
        "('queued', 'running', 'succeeded', 'failed', 'canceled')"
    )
    op.execute(
        '''
        CREATE TABLE "jobs" (
            id uuid PRIMARY KEY,
            handler text NOT NULL,
            arguments jsonb NOT NULL DEFAULT '{}'::jsonb,
            status "jobStatus" NOT NULL,
            result jsonb,
            error text,
            "createdAt" timestamptz NOT NULL,
            "startedAt" timestamptz,
            "finishedAt" timestamptz
        )
        '''
    )
    op.execute(
        'CREATE INDEX "jobsStatusCreatedIdx" ON "jobs" (status, "createdAt")'
    )


def downgrade() -> None:
    op.execute('DROP TABLE "jobs"')
    op.execute('DROP TYPE "jobStatus"')
