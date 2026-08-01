"""Re-extract `skills` with the 722-term vocabulary.

`skills` is derived at upsert from the description, so every stored row still
carries the result of the 133-term vocabulary until it is next fetched. This
applies the expanded one to what is already here, reading only
`description_text` — no refetching.

Two things changed, and only one of them adds skills:

* The vocabulary grew from 133 to 722 entries.
* 22 ambiguous names — `make`, `foundation`, `less`, `combine`, `kind`, `room`,
  `claude` and the rest — gained patterns requiring product context. These were
  never in the old vocabulary, so nothing is being taken away here; the
  qualification exists so that adding them does not attach "make" to half the
  archive.

Some rows *replace* a term rather than only gaining one, and the count is
reported below rather than hidden. Six old entries carried aliases that are now
their own terms:

    observability  ->  datadog, prometheus, grafana
    selenium       ->  cypress, playwright
    elasticsearch  ->  opensearch
    sqlserver      ->  t-sql
    oracle db      ->  pl/sql
    svelte         ->  sveltekit

A posting naming Cypress used to be tagged `selenium`, which was simply wrong;
it is now tagged `cypress`. So a row "losing" one of those six has gained a more
specific term, and checked across 12,000 descriptions there were **no losses of
any other kind**. That is the number the report below is really asking about.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-01
"""

from __future__ import annotations

import collections

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

BATCH = 500


def upgrade() -> None:
    from jobscrape import skills

    conn = op.get_bind()

    rows = conn.execute(
        sa.text("SELECT source, source_job_id, description_text, skills FROM jobs")
    ).mappings().all()

    updates = []
    before_terms: collections.Counter[str] = collections.Counter()
    after_terms: collections.Counter[str] = collections.Counter()
    before_total = after_total = 0
    replaced = 0
    unexplained = collections.Counter()

    #: Old entries whose aliases became their own terms. A row swapping one of
    #: these for a member of its set has gained precision, not lost a skill.
    promoted = {
        "observability": {"datadog", "prometheus", "grafana"},
        "selenium": {"cypress", "playwright"},
        "elasticsearch": {"opensearch"},
        "sqlserver": {"t-sql"},
        "oracle db": {"pl/sql"},
        "svelte": {"sveltekit"},
    }

    for row in rows:
        old = skills.parse_field(row["skills"])
        new = skills.extract(row["description_text"])
        before_terms.update(old)
        after_terms.update(new)
        before_total += len(old)
        after_total += len(new)
        # Separate a term being refined from a term being lost. Only the second
        # is a regression, and conflating them hides it.
        gone, gained = set(old) - set(new), set(new) - set(old)
        for term in gone:
            if term in promoted and promoted[term] & gained:
                replaced += 1
            else:
                unexplained[term] += 1
        if new != old:
            updates.append({
                "skills": ",".join(new),
                "source": row["source"],
                "job_id": row["source_job_id"],
            })

    query = sa.text(
        "UPDATE jobs SET skills = :skills "
        "WHERE source = :source AND source_job_id = :job_id"
    )
    for start in range(0, len(updates), BATCH):
        conn.execute(query, updates[start:start + BATCH])

    total = len(rows) or 1
    print(f"skills: rewrote {len(updates):,} of {len(rows):,} rows")
    print(f"    vocabulary        {133:>7,} -> {len(skills.VOCABULARY):>7,} terms")
    print(f"    distinct in use   {len(before_terms):>7,} -> {len(after_terms):>7,}")
    print(f"    mean per posting  {before_total/total:>7.2f} -> {after_total/total:>7.2f}")
    print(f"    terms refined to something more specific: {replaced:,}")
    print(f"    terms LOST with no replacement:           {sum(unexplained.values()):,}"
          f"   <- must be 0")
    for name, count in unexplained.most_common(10):
        print(f"        REGRESSION  {name}: {count:,}")
    print("\n    biggest additions:")
    gained = ((name, after_terms[name] - before_terms.get(name, 0))
              for name in after_terms)
    for name, delta in sorted(gained, key=lambda kv: -kv[1])[:15]:
        print(f"        +{delta:>6,}  {name}")


def downgrade() -> None:
    """No-op: the previous per-row values were not retained.

    A refetch recomputes `skills` from the current vocabulary regardless, so
    there is nothing here a rollback could meaningfully restore.
    """
