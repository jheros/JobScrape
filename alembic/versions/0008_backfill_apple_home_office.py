"""Backfill `is_remote` for Apple from the stored `homeOffice` flag.

0007 re-derived `workplace_type` from stored data, replaying each source's own
workplace field out of `raw_json`. Apple's fell through the gap: `homeOffice` is
a **boolean**, so it belongs in the `is_remote` column rather than in the string
hint 0007 reconstructs, and that column was never populated because the adapter
did not read the field until now. 876 Apple postings stayed `unknown` with the
answer sitting in their payload.

`homeOffice` was checked before being trusted. All 993 stored Apple rows read
False, which is exactly what a vestigial field looks like — a live fetch is what
settled it, returning True for 2 of 417. Rare but real, which matches how few
remote roles Apple advertises.

A False stays weak evidence on purpose: `classify_workplace` consults the
boolean only after the wording and anchored-phrase tiers, so an Apple posting
whose description states a hybrid schedule still reads hybrid, and one that
mentions remote work still reads remote. It decides only the postings that say
nothing at all.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-31
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

BATCH = 500


def _home_office(raw) -> bool | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, dict):
        return None
    value = raw.get("homeOffice")
    return value if isinstance(value, bool) else None


def upgrade() -> None:
    from jobscrape.models import classify_workplace

    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT source_job_id, title, location_raw, description_text, "
            "       workplace_type, raw_json "
            "FROM jobs WHERE source = 'apple'"
        )
    ).mappings().all()

    updates = []
    for row in rows:
        flag = _home_office(row["raw_json"])
        if flag is None:
            continue
        signal = " ".join(
            part for part in (
                row["title"], row["location_raw"],
                (row["description_text"] or "")[:1500],
            ) if part
        )
        full = " ".join(
            part for part in (
                row["title"], row["location_raw"], row["description_text"],
            ) if part
        )
        verdict = classify_workplace(signal, flag, None, full_text=full)
        if flag != row.get("is_remote") or verdict != (row["workplace_type"] or "unknown"):
            updates.append({
                "r": flag, "w": verdict, "job_id": row["source_job_id"],
            })

    query = sa.text(
        "UPDATE jobs SET is_remote = :r, workplace_type = :w "
        "WHERE source = 'apple' AND source_job_id = :job_id"
    )
    for start in range(0, len(updates), BATCH):
        conn.execute(query, updates[start:start + BATCH])

    after = conn.execute(
        sa.text(
            "SELECT workplace_type AS w, count(*) AS n FROM jobs "
            "WHERE source = 'apple' AND closed_at IS NULL AND duplicate_of IS NULL "
            "GROUP BY w ORDER BY n DESC"
        )
    ).mappings().all()
    print(f"apple: set is_remote and re-derived {len(updates):,} of {len(rows):,} rows")
    for row in after:
        print(f"    {str(row['w']):10} {row['n']:>6,}")


def downgrade() -> None:
    """No-op: the previous per-row values were not retained."""
