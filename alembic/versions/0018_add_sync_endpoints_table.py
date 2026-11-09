"""Add the `sync_endpoints` table — external HTTP receivers for collected jobs.

`db.SCHEMA` also declares this table, so a fresh install gets it via `0001`'s
replay, and `_schema_is_current()` now checks for it so an already-running
Docker deployment creates it on its next `connect()`. This revision brings the
migration history in line, the same way `0014` did for `schedules`.

No data to backfill: the Endpoints page is how rows get created.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    # `IF NOT EXISTS`: `db.connect()` may already have created it.
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS sync_endpoints (
            id                   BIGSERIAL PRIMARY KEY,
            name                 TEXT NOT NULL UNIQUE,
            url                  TEXT NOT NULL,
            token                TEXT NOT NULL,
            enabled              BOOLEAN NOT NULL DEFAULT TRUE,
            cursor_changed_at    TEXT,
            cursor_source        TEXT,
            cursor_source_job_id TEXT,
            last_sync_at         TIMESTAMPTZ,
            last_sync_status     TEXT,
            last_sync_message    TEXT NOT NULL DEFAULT '',
            created_at           TIMESTAMPTZ NOT NULL,
            updated_at           TIMESTAMPTZ NOT NULL
        )
    """))


def downgrade() -> None:
    op.drop_table("sync_endpoints")
