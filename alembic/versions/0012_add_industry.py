"""Add `jobs.industry` and backfill it from the payloads already stored.

The employer's **market** ("Information Technology and Services"), which is a
different facet from `department`, the **team or function** a role sits in
("Platform Engineering"). Four sources state it and three of them have been
parsing it and discarding it since the beginning:

    smartrecruiters  raw_json->'industry'->>'label'   {"id": "it_and_services", …}
    workable         raw_json->>'industry'            "Information Technology and Services"
    jobvite          raw_json->>'industry'            "Intel/Analysis"   (schema.org JobPosting)
    linkedin/apify   `sector`                         new; nothing stored yet to backfill

No refetching: every value below is read out of `raw_json`, which is exactly the
argument for storing the payload in the first place.

`department` is deliberately untouched. Indeed's `jobCategory` and every ATS's
department field are functions, not markets, and moving them here would corrupt
the column they are already correct in. The one mis-mapping being corrected is
LinkedIn's `sector`, which the mapper used to write into `department` — and no
LinkedIn row exists yet, so there is nothing here to repair.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

#: Both stored shapes in one expression: SmartRecruiters nests the value under
#: `label`, Workable and Jobvite store a bare string. `jsonb_typeof` is what
#: keeps the object form from being read as text (which would store the whole
#: `{"id": …, "label": …}` blob) and the string form from being read as an
#: object (which would silently yield NULL).
_VALUE = """
    CASE jsonb_typeof(raw_json -> 'industry')
        WHEN 'object' THEN raw_json -> 'industry' ->> 'label'
        WHEN 'string' THEN raw_json ->> 'industry'
    END
"""


def upgrade() -> None:
    conn = op.get_bind()

    # `IF NOT EXISTS` because `db.SCHEMA` also declares this column: a database
    # created by `db.connect()` and then migrated would otherwise fail here,
    # which is the ordinary path for a fresh install.
    conn.execute(sa.text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS industry TEXT"))

    before = conn.execute(
        sa.text("SELECT count(*) AS total, count(industry) AS filled FROM jobs")
    ).mappings().one()

    # NULLIF keeps a blank out of the column: an empty string reads as "the
    # industry is known and it is nothing", which is not what a missing label
    # means. `industry IS NULL` is what makes a second run a no-op.
    result = conn.execute(sa.text(f"""
        UPDATE jobs
           SET industry = NULLIF(btrim({_VALUE}), '')
         WHERE industry IS NULL
           AND raw_json IS NOT NULL
           AND jsonb_typeof(raw_json -> 'industry') IN ('object', 'string')
           -- Without this, the 129 rows carrying an EMPTY label match on every
           -- run and get rewritten NULL-over-NULL: a no-op in the data and a
           -- dead row apiece in the table. With it, a second run touches zero.
           AND NULLIF(btrim({_VALUE}), '') IS NOT NULL
    """))

    after = conn.execute(
        sa.text("SELECT count(industry) AS filled FROM jobs")
    ).mappings().one()

    total = before["total"] or 1
    print(f"industry: backfilled {result.rowcount:,} of {before['total']:,} rows")
    print(f"    populated  {before['filled']:,} -> {after['filled']:,} "
          f"({after['filled'] / total:.1%})")

    by_source = conn.execute(sa.text(
        "SELECT source, count(*) AS n FROM jobs "
        "WHERE industry IS NOT NULL GROUP BY source ORDER BY n DESC"
    )).mappings().all()
    for row in by_source:
        print(f"        {row['source']:<18}{row['n']:>7,}")

    top = conn.execute(sa.text(
        "SELECT industry, count(*) AS n FROM jobs "
        "WHERE industry IS NOT NULL GROUP BY industry ORDER BY n DESC LIMIT 10"
    )).mappings().all()
    print("    most common:")
    for row in top:
        print(f"        {row['n']:>7,}  {row['industry']}")


def downgrade() -> None:
    """Drops the column. The values are all still in `raw_json`, so `upgrade`
    reconstructs them exactly — nothing here is one-way."""
    op.drop_column("jobs", "industry")
