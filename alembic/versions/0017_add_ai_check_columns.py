"""Add `jobs.ai_*` columns for the `ai-check` second-opinion stage.

`db.SCHEMA` also declares these columns now, so a fresh install gets them
for free via `0001`'s replay, and `_migrate()`'s self-heal adds them to
already-connected databases the same way `0016`'s docstring describes for
`duplicate_marked_at`. This revision brings the migration history in line
with what `_migrate()` already does silently.

No data to backfill: `ai_checked_at` NULL means "never checked", which is
exactly what every existing row should read as.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    # `IF NOT EXISTS` for the same reason as `0016`: `db.SCHEMA` declares
    # these columns too, and `_migrate()`'s self-heal may have already added
    # them to this database on a prior connect.
    conn.execute(sa.text(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ai_workplace_type TEXT"))
    conn.execute(sa.text(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ai_requires_clearance BOOLEAN"))
    conn.execute(sa.text(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ai_is_us BOOLEAN"))
    conn.execute(sa.text(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ai_employment_type TEXT"))
    conn.execute(sa.text(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ai_seniority TEXT"))
    conn.execute(sa.text(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ai_checked_at TIMESTAMPTZ"))
    conn.execute(sa.text(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ai_model TEXT"))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_jobs_ai_checked ON jobs (ai_checked_at)"))


def downgrade() -> None:
    op.drop_index("idx_jobs_ai_checked", table_name="jobs")
    op.drop_column("jobs", "ai_model")
    op.drop_column("jobs", "ai_checked_at")
    op.drop_column("jobs", "ai_seniority")
    op.drop_column("jobs", "ai_employment_type")
    op.drop_column("jobs", "ai_is_us")
    op.drop_column("jobs", "ai_requires_clearance")
    op.drop_column("jobs", "ai_workplace_type")
