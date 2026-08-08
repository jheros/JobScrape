"""Add `schedules.interval_days` and `jobs.render_synced_at`/`render_sync_status`.

`db.SCHEMA` also declares all three columns now, so a fresh install gets
them for free via `0001`'s replay. This revision is what reaches an
EXISTING database — same gap `0013`/`0014` closed for `companies`/
`schedules`, now for these columns.

`interval_days` (nullable, `schedules`): lets a schedule fire "every N
days" via an `IntervalTrigger` instead of the cron fields — see
`scheduler.reload()`. NULL means "use the cron fields as before".

`render_synced_at`/`render_sync_status` (nullable, `jobs`): per-row
bookkeeping `render_sync.py`'s `sync()` now stamps on every batch flush,
so the Jobs dashboard can show whether a row has actually reached Render
rather than only knowing the sync job's own run-level watermark.

No data to backfill: existing schedules keep using their cron fields
(`interval_days` NULL), and existing jobs simply read as "pending" until
the next `render-sync` run stamps them for real.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(
        "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS interval_days INTEGER"
    ))
    conn.execute(sa.text(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS render_synced_at TIMESTAMPTZ"
    ))
    conn.execute(sa.text(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS render_sync_status TEXT"
    ))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("ALTER TABLE schedules DROP COLUMN IF EXISTS interval_days"))
    conn.execute(sa.text("ALTER TABLE jobs DROP COLUMN IF EXISTS render_synced_at"))
    conn.execute(sa.text("ALTER TABLE jobs DROP COLUMN IF EXISTS render_sync_status"))
