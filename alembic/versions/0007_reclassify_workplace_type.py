"""Re-derive `workplace_type` after four fixes to how it is decided.

0006 did this once for the permissive remote sweep. It only ever *filled*
blanks, because at the time every change was additive. This one must also
**correct** rows, so it writes wherever the verdict changes:

* A shallow refresh used to overwrite a good verdict with "unknown". The upsert
  now treats "unknown" like NULL, but the damage is already in the table — 178
  Workday postings store `remoteType: Hybrid` in their payload and read
  "unknown", and 0006 had already fixed those same rows once before a later
  sweep ate them again.
* The source's own workplace field is now mapped by value rather than by
  substring. Workday ships "Remote work is not available", which contains the
  word and so read as *remote*. Unlike 0006, this tier CAN be replayed here:
  `workplace_hint` is not a stored column, but the value is still in `raw_json`
  for the sources that supply it.
* An anchored-phrase tier now reads the whole description — "3 days in the
  office", "must be on-site", "fully remote" — above the remote sweep. Hybrid
  and onsite previously had no whole-document tier at all.
* "virtual" is out of `REMOTE_MENTION`. 211 postings were remote solely because
  they mentioned a virtual interview; those return to unknown, which is the
  point rather than a regression.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-31
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

BATCH = 500

#: Where each source keeps its own workplace field inside `raw_json`. Only
#: sources that actually have one — everything else replays from text alone.
HINT_KEYS = {
    "workday": ("remoteType",),
    "oracle": ("WorkplaceType",),
    "ashby": ("workplaceType",),
    "lever": ("workplaceType",),
    "usajobs": ("RemoteIndicator",),
}


def _distribution(conn) -> dict[str, int]:
    rows = conn.execute(
        sa.text(
            "SELECT workplace_type AS w, count(*) AS n FROM jobs "
            "WHERE closed_at IS NULL AND duplicate_of IS NULL GROUP BY w"
        )
    ).mappings().all()
    return {str(r["w"]): r["n"] for r in rows}


def _hint(source: str, raw) -> str | None:
    """Pull the source's native workplace value back out of the stored payload."""
    keys = HINT_KEYS.get(source)
    if not keys or not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, dict):
        return None
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, bool):
            return "remote" if value else None
    return None


def upgrade() -> None:
    from jobscrape.models import classify_workplace

    conn = op.get_bind()
    before = _distribution(conn)

    # Every row, not just the unknown ones: parts 2 and 4 change answers that
    # are currently wrong, so restricting to blanks would leave them wrong.
    rows = conn.execute(
        sa.text(
            "SELECT source, source_job_id, title, location_raw, description_text, "
            "       is_remote, workplace_type, raw_json "
            "FROM jobs"
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
        verdict = classify_workplace(
            signal, row["is_remote"], _hint(row["source"], row["raw_json"]), full_text=full
        )
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
    print(f"workplace_type: changed {len(updates):,} of {len(rows):,} stored rows")
    for key in sorted(set(before) | set(after)):
        print(f"    {key:10} {before.get(key, 0):>7,} -> {after.get(key, 0):>7,}")


def downgrade() -> None:
    """No-op: the previous per-row values were not retained.

    Re-running a fetch recomputes `workplace_type` from the current classifier
    regardless, so there is nothing here that a rollback could meaningfully
    restore.
    """
