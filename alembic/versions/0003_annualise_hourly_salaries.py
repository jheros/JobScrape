"""Annualise hourly pay so `salary_min`/`salary_max` mean one thing.

There is no `salary_period` column, so nothing downstream could tell a stored
`22.0` from a stored `220000.0`. 488 of 29,898 salaried rows — across lever,
greenhouse, workable, jooble, ashby, workday, adzuna, smartrecruiters, usajobs,
himalayas and hackernews — held a raw hourly rate in the annual column. Every
salary filter, sort and export compared the two, and the hourly rows sorted to
the bottom as if they paid nothing.

Two shapes are corrected:

* **31 rows with `salary_min = 0`** — zero is not a pay figure, it is a source
  sending an unset field as a number. These are cleared. 13 of them carry a
  real `salary_max`, which is kept: a stated ceiling with no floor is still
  information, whereas a fabricated floor of 0 is not.
* **457 rows in (0, 1000)** — scaled by 2,080 hours (40 × 52). Sampling these
  leaves no ambiguity: a "Software Engineer 2" at 64.04–120.27 and a "Student
  Trainee" at 17.45–22.68 are hourly rates, not annual salaries. Roughly half
  say so in their description; the rest arrived through structured API salary
  fields whose period the adapters discarded.

The threshold is deliberate rather than tuned. No US annual software salary
sits below $1,000, and no hourly rate that survived `parse_salary`'s 7–500 band
reaches it, so the two populations do not overlap and nothing is misclassified
by the split. Measured: **0 rows** have `salary_min < 1000` with
`salary_max >= 1000`, i.e. no row straddles the boundary.

`salary_raw` is untouched throughout — the text as published stays on record,
so the conversion is auditable and reversible per row.

Not reversible in `downgrade`: dividing every sub-$45,760 salary by 2,080 would
also hit genuine low annual salaries that were never scaled. The downgrade
restores the *schema*, which is unchanged, and deliberately leaves the data
converted rather than corrupting rows it cannot identify.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

#: Must match `jobscrape.models.HOURS_PER_YEAR`.
HOURS_PER_YEAR = 2080


def upgrade() -> None:
    # Order matters: clear the zeros first so they are not scaled to zero again
    # and then counted as converted.
    op.execute(
        """
        UPDATE jobs
           SET salary_min = NULL,
               salary_currency = CASE WHEN salary_max IS NULL OR salary_max = 0
                                      THEN NULL ELSE salary_currency END,
               salary_max = CASE WHEN salary_max = 0 THEN NULL ELSE salary_max END
         WHERE salary_min = 0
        """
    )
    op.execute(
        f"""
        UPDATE jobs
           SET salary_min = salary_min * {HOURS_PER_YEAR},
               salary_max = CASE WHEN salary_max IS NULL THEN NULL
                                 ELSE salary_max * {HOURS_PER_YEAR} END
         WHERE salary_min > 0 AND salary_min < 1000
        """
    )


def downgrade() -> None:
    """No-op: see the module docstring. The schema is unchanged."""
