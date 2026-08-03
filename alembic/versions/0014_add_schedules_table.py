"""Add the `schedules` table — the dashboard's in-process scheduler.

`db.SCHEMA` also declares this table now, so a fresh install gets it for free
via `0001`'s replay. This revision is what reaches an EXISTING database:
`_schema_is_current()` only checked `jobs`/`companies` before this change, so
an already-current install would otherwise never see `schedules` created —
the same gap the `zero_yield_streak`/`quiet_until` columns closed in `0013`
for `companies`, now for a whole new table instead of new columns.

No data to backfill: the dashboard starts with zero schedules and the
Schedule page is how they get created.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    # `IF NOT EXISTS` for the same reason as `0013`: `db.SCHEMA` declares this
    # table too, so a database created fresh by `db.connect()` and then
    # migrated must not fail here.
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS schedules (
            id               SERIAL PRIMARY KEY,
            name             TEXT NOT NULL,
            stage            TEXT NOT NULL,
            extra            TEXT,
            cron_minute      TEXT NOT NULL DEFAULT '0',
            cron_hour        TEXT NOT NULL DEFAULT '7',
            cron_day_of_week TEXT NOT NULL DEFAULT '*',
            timezone         TEXT NOT NULL DEFAULT 'Asia/Bangkok',
            enabled          BOOLEAN NOT NULL DEFAULT TRUE,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_run_id      BIGINT,
            last_fired_at    TIMESTAMPTZ
        )
    """))


def downgrade() -> None:
    op.drop_table("schedules")
