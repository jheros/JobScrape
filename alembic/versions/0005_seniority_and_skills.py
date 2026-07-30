"""Add `seniority` and `skills`, completing the nine-field job contract.

Every job should carry at least: title, source, location, mode, seniority,
salary, skills, description and link. Seven of those already existed; these are
the two that did not.

**`seniority`** derives from the title alone (see `jobscrape/seniority.py`).
That is why it is an ordinary `Job` dataclass column rather than an
upsert-computed one: `title` is required, always present, and never blanked by
`shallow_refresh`, so it recomputes correctly on every pass with no guard.
Backfilled here; measured to classify 54.7% of open postings, with the rest
honestly `unknown` — a bare "Software Engineer" names no level.

**`skills`** derives from the description (see `jobscrape/skills.py`) and so
follows the `evergreen_text` / `scam_signals` pattern exactly, including the
`CASE WHEN excluded.description_text IS NOT NULL` guard in `upsert_jobs`.
Without that guard a repeat sweep — which sends a shallow refresh carrying no
description — would empty the column for every already-known posting.

The backfill runs in Python, not SQL: the vocabulary is 133 word-boundary
anchored patterns with alias folding, which is not one regex.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

#: Rows per UPDATE round-trip during the backfill. The archive is ~44k rows and
#: each carries a description up to 12k chars, so batching keeps the memory
#: flat without making this a per-row chatter-fest.
BATCH = 500


def upgrade() -> None:
    op.add_column("jobs", sa.Column("seniority", sa.Text(), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("skills", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("idx_jobs_seniority", "jobs", ["seniority"])
    # GIN over the parsed list — a leading-wildcard LIKE on the stored
    # comma-separated string cannot be indexed, and was slow enough to hang a
    # dashboard page once the staleness cluster join was applied on top.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_skills "
        "ON jobs USING GIN (string_to_array(skills, ','))"
    )

    from jobscrape import seniority, skills

    conn = op.get_bind()

    # --- seniority: from the title, so every row gets a value ---
    rows = conn.execute(
        sa.text("SELECT source, source_job_id, title FROM jobs")
    ).mappings().all()
    updates = [
        {"level": seniority.classify(row["title"]),
         "source": row["source"], "job_id": row["source_job_id"]}
        for row in rows
    ]
    _apply(conn, "UPDATE jobs SET seniority = :level "
                 "WHERE source = :source AND source_job_id = :job_id", updates)
    classified = sum(1 for u in updates if u["level"] != seniority.UNKNOWN)
    print(f"seniority backfill: {classified:,} of {len(updates):,} classified "
          f"({100 * classified / max(len(updates), 1):.1f}%)")

    # --- skills: from the description ---
    rows = conn.execute(
        sa.text("SELECT source, source_job_id, description_text FROM jobs "
                "WHERE description_text IS NOT NULL")
    ).mappings().all()
    updates = [
        {"skills": skills.to_field(row["description_text"]),
         "source": row["source"], "job_id": row["source_job_id"]}
        for row in rows
    ]
    updates = [u for u in updates if u["skills"]]
    _apply(conn, "UPDATE jobs SET skills = :skills "
                 "WHERE source = :source AND source_job_id = :job_id", updates)
    print(f"skills backfill: {len(updates):,} of {len(rows):,} descriptions "
          f"yielded at least one skill")


def _apply(conn, statement: str, updates: list[dict]) -> None:
    query = sa.text(statement)
    for start in range(0, len(updates), BATCH):
        conn.execute(query, updates[start:start + BATCH])


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_jobs_skills")
    op.drop_index("idx_jobs_seniority", table_name="jobs")
    op.drop_column("jobs", "skills")
    op.drop_column("jobs", "seniority")
