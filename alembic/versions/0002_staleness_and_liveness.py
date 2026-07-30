"""Add evergreen_text (staleness evidence) and liveness_strikes.

`evergreen_text` — does the description read as a standing advertisement
("talent pool", "always hiring") rather than a specific opening? Stored because
it is *evidence*, not a verdict: it depends only on the description text, never
on the clock, so unlike the staleness verdict itself it does not go stale.
Matching the phrases at read time with `ILIKE ANY` was measured at **52 seconds
per query** across the open catalogue — leading wildcards cannot use an index.

`liveness_strikes` — consecutive "expired" probe reads. A job closes at two;
any healthy read resets it, and a reopen resets it too so the two-strike grace
survives one.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS "
        "evergreen_text BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS "
        "liveness_strikes INTEGER NOT NULL DEFAULT 0"
    )
    # Partial: the flag is true on a small minority of rows, and every query
    # that cares is already scoped to open jobs.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_evergreen ON jobs (evergreen_text) "
        "WHERE closed_at IS NULL AND evergreen_text"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_jobs_evergreen")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS liveness_strikes")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS evergreen_text")
