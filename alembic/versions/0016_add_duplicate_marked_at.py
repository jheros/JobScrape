"""Add `jobs.duplicate_marked_at`.

`db.SCHEMA` also declares this column now, so a fresh install gets it for
free via `0001`'s replay, and `_migrate()`'s self-heal has been adding it
to already-connected databases since the column was introduced — but it
never got its own revision the way `0013`/`0014`/`0015` gave the columns
and table they closed the same gap for. This revision brings the migration
history in line with what `_migrate()` has already been doing silently.

No data to backfill: it records the moment a row stops being a suppressed
duplicate, so existing rows correctly start out NULL.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    # `IF NOT EXISTS` for the same reason as `0013`/`0014`/`0015`: `db.SCHEMA`
    # declares this column too, and `_migrate()`'s self-heal may have already
    # added it to this database on a prior connect.
    conn.execute(sa.text(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS duplicate_marked_at TIMESTAMPTZ"
    ))


def downgrade() -> None:
    op.drop_column("jobs", "duplicate_marked_at")
