"""The `Job` dataclass, `JOB_COLUMNS` and `SCHEMA` must agree.

`JOB_COLUMNS` is hand-listed and is the only thing that decides whether a field
reaches the database. Nothing about forgetting it fails loudly: the column is
simply never written, the adapter still looks correct, and the data is missing
only when somebody goes looking for it months later.

This is the same class of guard as `test_preserved_set_matches_the_upsert`
(`tests/test_incremental.py`) and the staleness Python-vs-SQL drift test — three
places where two hand-maintained lists have to stay in step.
"""

from __future__ import annotations

import dataclasses
import re

from jobscrape import db
from jobscrape.models import Job

#: Dataclass fields that deliberately never become columns.
#:
#: `raw` is stored under a different name (`raw_json`, JSON-encoded), and
#: `workplace_hint` is an *input* to classification that is discarded once
#: `workplace_type` has been derived from it.
NOT_COLUMNS = {"raw", "workplace_hint"}


def _schema_columns() -> list[str]:
    """Column names declared in `SCHEMA`'s `jobs` table."""
    body = db.SCHEMA.split("CREATE TABLE IF NOT EXISTS jobs")[1]
    body = body.split(");")[0]
    names = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("--") or line.startswith("("):
            continue
        match = re.match(r"([a-z_]+)\s+(TEXT|BOOLEAN|INTEGER|DOUBLE|TIMESTAMPTZ|JSONB|BIGSERIAL|SERIAL)", line)
        if match:
            names.append(match.group(1))
    return names


def test_job_columns_covers_every_stored_dataclass_field() -> None:
    """A field on `Job` that is missing from `JOB_COLUMNS` is silently dropped."""
    fields = {f.name for f in dataclasses.fields(Job)} - NOT_COLUMNS
    missing = fields - set(db.JOB_COLUMNS)
    assert not missing, (
        f"{sorted(missing)} are on the Job dataclass but not in JOB_COLUMNS, "
        "so they never reach the database"
    )


def test_job_columns_has_nothing_the_dataclass_cannot_supply() -> None:
    """The reverse: a column listed here must have a value to write."""
    fields = {f.name for f in dataclasses.fields(Job)}
    # `raw_json` is the stored name for the `raw` field; `to_row()` renames it.
    supplied = (fields - NOT_COLUMNS) | {"raw_json"}
    orphans = set(db.JOB_COLUMNS) - supplied
    assert not orphans, f"{sorted(orphans)} are in JOB_COLUMNS but nothing sets them"


def test_every_job_column_exists_in_the_schema() -> None:
    """`JOB_COLUMNS` naming a column `SCHEMA` lacks is an INSERT that errors."""
    declared = set(_schema_columns())
    assert declared, "could not parse the jobs table out of SCHEMA"
    missing = set(db.JOB_COLUMNS) - declared
    assert not missing, f"{sorted(missing)} are in JOB_COLUMNS but not in SCHEMA"


def test_to_row_produces_exactly_the_stored_columns() -> None:
    """End-to-end: what a Job serialises to must match what gets inserted."""
    job = Job(
        source="t", source_job_id="1", title="Senior Software Engineer",
        company="Acme", job_url="https://example.com/1",
    )
    row = job.to_row()
    assert set(row) == set(db.JOB_COLUMNS), (
        f"to_row() and JOB_COLUMNS disagree: "
        f"extra={sorted(set(row) - set(db.JOB_COLUMNS))} "
        f"missing={sorted(set(db.JOB_COLUMNS) - set(row))}"
    )


def test_derived_columns_are_written_and_guarded() -> None:
    """`evergreen_text`, `scam_signals` and `skills` are computed at upsert.

    Each derives from `description_text`, which is COALESCE-preserved, so each
    needs a `CASE WHEN excluded.description_text IS NOT NULL` guard — otherwise
    a shallow refresh (which carries no description) recomputes it from nothing
    and wipes the stored value. Asserted by reading the SQL the function builds,
    the same technique `test_preserved_set_matches_the_upsert` uses.
    """
    import inspect

    source = inspect.getsource(db.upsert_jobs)
    for column in ("evergreen_text", "scam_signals", "skills"):
        assert f'"{column}=CASE WHEN excluded.description_text IS NOT NULL "' in source, (
            f"{column} is derived from description_text but has no shallow-refresh "
            "guard in upsert_jobs; a repeat sweep would erase it"
        )


def test_seniority_needs_no_refresh_guard() -> None:
    """It derives from `title`, which is required and never blanked.

    Documents *why* seniority is plumbed differently from skills — the two are
    adjacent in the code and the asymmetry is otherwise easy to read as an
    oversight.
    """
    from jobscrape.sources.base import PRESERVED_ON_REFRESH, shallow_refresh

    assert "seniority" not in PRESERVED_ON_REFRESH
    job = Job(source="t", source_job_id="1", title="Staff Engineer",
              company="Acme", job_url="https://x/1")
    assert shallow_refresh(job).seniority == "staff", (
        "a shallow refresh must still carry the level, since the title survives"
    )
