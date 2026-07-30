"""Add `scam_signals`: advance-fee / identity-harvest evidence.

Stored for the same reason as `evergreen_text` — it derives from the
description alone and never from the clock, so unlike a staleness verdict it
cannot go stale between backfills — and scanning for it at read time would
repeat the 52-second full-text problem that column was introduced to solve.

The backfill runs the three patterns over every stored description. It is
expected to set **nothing**: all three matched zero of 44,493 descriptions when
they were chosen, which is precisely why these three were kept and the other six
JobSentinel patterns were dropped (see scam.py). An empty column here is the
check working, not the check failing.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("scam_signals", sa.Text(), nullable=False, server_default=""),
    )

    # Backfill in Python rather than SQL: the patterns include a
    # warning-context guard (an employer describing the scam in order to warn
    # candidates about it must not be flagged as running it), which is not
    # expressible as a single regex.
    from jobscrape import scam

    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT source, source_job_id, description_text FROM jobs "
            "WHERE description_text IS NOT NULL"
        )
    ).mappings()

    flagged = 0
    for row in rows:
        signals = scam.scam_signals(row["description_text"])
        if not signals:
            continue
        flagged += 1
        conn.execute(
            sa.text(
                "UPDATE jobs SET scam_signals = :signals "
                "WHERE source = :source AND source_job_id = :job_id"
            ),
            {
                "signals": ",".join(signals),
                "source": row["source"],
                "job_id": row["source_job_id"],
            },
        )
    print(f"scam_signals backfill: {flagged} posting(s) flagged")


def downgrade() -> None:
    op.drop_column("jobs", "scam_signals")
