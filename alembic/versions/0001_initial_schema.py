"""Initial schema — the archive as productionized: jobs/companies/runs/
run_events/posting_seen/job_status, plus closed_at (grace-window closure),
duplicate_of (repost/cross-source collapse), and board_health columns.

Deliberately reuses `jobscrape.db.SCHEMA`/`INDEXES` rather than restating the
DDL here — those strings are the single source of truth db.py itself applies
on every connect() (as a `CREATE ... IF NOT EXISTS` safety net for zero-config
dev/test use), so this revision and the app's own schema-ensure step can never
drift apart.

Revision ID: 0001
Revises:
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

from jobscrape.db import INDEXES, SCHEMA, _strip_sql_comments

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(SCHEMA)
    for statement in filter(None, (s.strip() for s in _strip_sql_comments(INDEXES).split(";"))):
        op.execute(statement)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS run_events;
        DROP TABLE IF EXISTS runs;
        DROP TABLE IF EXISTS job_status;
        DROP TABLE IF EXISTS posting_seen;
        DROP TABLE IF EXISTS jobs;
        DROP TABLE IF EXISTS companies;
    """)
