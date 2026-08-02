"""Add `companies.zero_yield_streak` / `quiet_until` — back off jobless boards.

The sibling of the `consecutive_failures` / `cooldown_until` pair added for
board_health, and deliberately separate from it. That backs off a board that is
*failing*. This backs off a board that works perfectly and simply never carries
a job this archive wants.

Paycom is why. All 5,135 of its discovered portals are car dealerships, clinics
and municipalities; a live sample served 55 postings of which the role prefilter
passed exactly none ("Corporate Staff Accountant", "Forklift Mechanic",
"Industrial Dock/Door Install"). Every sweep spent roughly 10,000 requests there
and stored nothing — and those requests are the budget the 26,390-board
validation backlog needs.

Backoff, not exclusion: a dealership can hire a developer, the ceiling is two
weeks, and one kept posting resets a board to full frequency.

Both columns start at their resting values, so there is nothing to backfill —
the first sweep after this begins counting.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    # `IF NOT EXISTS` because `db.SCHEMA` also declares these: a database created
    # by `db.connect()` and then migrated would otherwise fail here, which is
    # the ordinary path for a fresh install.
    conn.execute(sa.text(
        "ALTER TABLE companies "
        "ADD COLUMN IF NOT EXISTS zero_yield_streak INTEGER NOT NULL DEFAULT 0"
    ))
    conn.execute(sa.text(
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS quiet_until TIMESTAMPTZ"
    ))

    # What the backoff will be measured against. Boards with no stored jobs are
    # the candidates; how many actually go quiet depends on three consecutive
    # sweeps, which is a fact about the future, not about this migration.
    rows = conn.execute(sa.text("""
        SELECT co.ats,
               count(*) AS boards,
               count(*) FILTER (WHERE j.n IS NULL) AS never_yielded
          FROM companies co
          LEFT JOIN (SELECT source, company_slug, count(*) AS n
                       FROM jobs GROUP BY source, company_slug) j
            ON j.source = co.ats AND j.company_slug = co.slug
         GROUP BY co.ats
         HAVING count(*) FILTER (WHERE j.n IS NULL) > 0
         ORDER BY 3 DESC
         LIMIT 10
    """)).mappings().all()
    print("yield backoff: boards with no stored job today (backoff candidates)")
    for row in rows:
        print(f"    {row['ats']:<18}{row['never_yielded']:>7,} of {row['boards']:>7,}")


def downgrade() -> None:
    op.drop_column("companies", "quiet_until")
    op.drop_column("companies", "zero_yield_streak")
