"""Turn a batch of JobScoutPro-scraped records into stored postings.

Modeled on `jobscrape.sources.apify.ingest`, minus the parts that don't apply
here: there's no run/dataset to look up on a remote API and no mapper to
resolve — the caller (the `jobright-ingest` CLI stage) already has the
records in hand, handed over by JobScoutPro's own scraper. Downstream of the
mapping, nothing here is new: `apply_filters_counted` decides what to keep
and `db.upsert_jobs` writes it, exactly as every other source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ... import db
from ...filters import apply_filters_counted
from . import mapper

log = logging.getLogger("jobscrape.jobright")


@dataclass
class IngestResult:
    """What one batch produced, in the shape the CLI and the log both want."""

    items: int = 0
    mapped: int = 0
    kept: int = 0
    inserted: int = 0
    updated: int = 0
    funnel: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": self.items, "mapped": self.mapped, "kept": self.kept,
            "inserted": self.inserted, "updated": self.updated,
            "funnel": self.funnel, "error": self.error,
        }

    def summary(self) -> str:
        if self.error:
            return f"jobright-ingest: {self.error}"
        return (
            f"jobright-ingest: {self.items} items, {self.mapped} mapped, "
            f"{self.kept} kept, {self.inserted} new, {self.updated} refreshed"
        )


def ingest_records(
    conn: db.Connection,
    records: list[dict[str, Any]],
    *,
    max_items: int | None = None,
    allow_roles: tuple[str, ...] = (),
    software_only: bool = True,
    us_only: bool = True,
    remote_only: bool = True,
) -> IngestResult:
    """Map, filter and store one batch of JobScoutPro records."""
    result = IngestResult(items=len(records))
    if max_items is not None:
        records = records[:max_items]
        result.items = len(records)
    if not records:
        return result

    jobs = mapper.to_jobs(records)
    result.mapped = len(jobs)
    if not jobs:
        return result

    kept, funnel = apply_filters_counted(
        jobs, software_only=software_only, us_only=us_only,
        remote_only=remote_only, allow_roles=allow_roles,
    )
    result.kept = len(kept)
    result.funnel = funnel.as_dict()
    if kept:
        result.inserted, result.updated = db.upsert_jobs(conn, kept)
    return result


def ingest_and_record(
    conn: db.Connection,
    records: list[dict[str, Any]],
    *,
    command: str = "jobright-ingest",
    **kwargs: Any,
) -> IngestResult:
    """`ingest_records` wrapped in the project's run bookkeeping.

    Recorded in `runs`/`run_events` so a jobright-ingest run appears on the
    dashboard's Runs page beside every other stage.
    """
    db_run = db.start_run(conn, command=command, stage="jobright")
    try:
        result = ingest_records(conn, records, **kwargs)
    except Exception as exc:                      # noqa: BLE001 - reported, not raised
        log.exception("jobright: ingesting %d records failed", len(records))
        db.log_event(conn, db_run, f"ingest failed: {exc}",
                     stage="jobright", source="jobright", level="error")
        db.finish_run(conn, db_run, status="error", error=str(exc))
        return IngestResult(items=len(records), error=str(exc))

    db.log_event(conn, db_run, result.summary(), stage="jobright",
                 source="jobright",
                 level="error" if result.error else "info",
                 counters=result.as_dict())
    db.finish_run(conn, db_run, status="error" if result.error else "ok",
                  stats_payload=result.as_dict(), error=result.error)
    log.info("jobright: %s", result.summary())
    return result
