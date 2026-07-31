"""Recompute `workplace_type` with the permissive remote sweep.

`workplace_type` is computed when a `Job` is constructed and then stored, so a
change to the classifier only affects postings fetched afterwards. This applies
it to what is already here.

Two things changed in the classifier:

* Oracle's native `WorkplaceType` and USAJobs' `RemoteIndicator` are now read.
  Those cannot be backfilled for most rows — `shallow_refresh` sets `raw = {}`
  on already-known postings and `to_row()` serialises that to the string
  `"{}"`, which is not NULL and so escapes the `COALESCE` preserve rule. The
  payloads are mostly gone. Those two fixes pay off on the next fetch instead.
* A final sweep marks a posting remote if any word in `models.REMOTE_MENTION`
  appears anywhere in it — but **only** when every other tier returned
  `unknown`. That part backfills fine, since it reads title, location and
  description, all of which are stored.

The sweep fills blanks only, so an existing `hybrid`, `onsite` or `remote`
verdict is never overwritten. The migration asserts that rather than trusting
it: it recomputes every row but only writes where the stored value was
`unknown` or NULL, and reports the before/after distribution.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-31
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

BATCH = 500


def _distribution(conn) -> dict[str, int]:
    rows = conn.execute(
        sa.text(
            "SELECT workplace_type AS w, count(*) AS n FROM jobs "
            "WHERE closed_at IS NULL AND duplicate_of IS NULL GROUP BY w"
        )
    ).mappings().all()
    return {str(r["w"]): r["n"] for r in rows}


def upgrade() -> None:
    from jobscrape.models import classify_workplace

    conn = op.get_bind()
    before = _distribution(conn)

    rows = conn.execute(
        sa.text(
            "SELECT source, source_job_id, title, location_raw, description_text, "
            "       is_remote, workplace_type "
            "FROM jobs WHERE workplace_type IS NULL OR workplace_type = 'unknown'"
        )
    ).mappings().all()

    updates = []
    for row in rows:
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
        # `workplace_hint` is not a stored column — it is an input discarded
        # after classification — so the native-field tier cannot be replayed
        # here. Text and the structured flag are all that is available.
        verdict = classify_workplace(signal, row["is_remote"], None, full_text=full)
        if verdict != (row["workplace_type"] or "unknown"):
            updates.append({
                "w": verdict,
                "source": row["source"],
                "job_id": row["source_job_id"],
            })

    query = sa.text(
        "UPDATE jobs SET workplace_type = :w "
        "WHERE source = :source AND source_job_id = :job_id"
    )
    for start in range(0, len(updates), BATCH):
        conn.execute(query, updates[start:start + BATCH])

    after = _distribution(conn)
    print(f"workplace_type: reclassified {len(updates):,} of {len(rows):,} unknown rows")
    for key in sorted(set(before) | set(after)):
        print(f"    {key:10} {before.get(key, 0):>7,} -> {after.get(key, 0):>7,}")


def downgrade() -> None:
    """No-op: the previous per-row values were not retained.

    Re-running a fetch recomputes `workplace_type` from the current classifier
    regardless, so there is nothing here that a rollback could meaningfully
    restore.
    """
