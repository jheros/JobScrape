"""Turn a batch of hiring.cafe search hits into stored postings.

Near-verbatim mirror of `jobscrape.sources.jobright.ingest`: there's no
run/dataset to look up on a remote API here either -- the caller (the
`hiringcafe-ingest` CLI stage, or `hiringcafe-fetch` in-process) already has
the records in hand. Downstream of the mapping, nothing here is new:
`apply_filters_counted` decides what to keep and `db.upsert_jobs` writes it,
exactly as every other source.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ... import db
from ...filters import apply_filters_counted
from . import mapper

log = logging.getLogger("jobscrape.hiringcafe")


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
            return f"hiringcafe-ingest: {self.error}"
        return (
            f"hiringcafe-ingest: {self.items} items, {self.mapped} mapped, "
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
    since: date | None = None,
    until: date | None = None,
) -> IngestResult:
    """Map, filter and store one batch of hiring.cafe search hits."""
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
        since=since, until=until,
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
    command: str = "hiringcafe-ingest",
    **kwargs: Any,
) -> IngestResult:
    """`ingest_records` wrapped in the project's run bookkeeping.

    Recorded in `runs`/`run_events` so a hiringcafe-ingest run appears on
    the dashboard's Runs page beside every other stage.
    """
    # The dashboard opens the run row before spawning this process and passes
    # its id through the environment (see `cmd_fetch`'s identical check) --
    # without this, every dashboard-launched hiringcafe-fetch would appear
    # twice in history: once as the dashboard's own row, stuck at "running"
    # forever, and once here.
    inherited = os.environ.get("JOBSCRAPE_RUN_ID")
    db_run = int(inherited) if inherited else db.start_run(
        conn, command=command, stage="hiringcafe")
    try:
        result = ingest_records(conn, records, **kwargs)
    except Exception as exc:                      # noqa: BLE001 - reported, not raised
        log.exception("hiringcafe: ingesting %d records failed", len(records))
        # A DB-level failure leaves Postgres refusing every further statement
        # on this connection until the transaction ends -- without this,
        # log_event/finish_run below would each raise InFailedSqlTransaction
        # in turn, masking the real error and leaving this run stuck at
        # status='running' forever. Harmless to call when the exception
        # wasn't DB-related too.
        conn.rollback()
        db.log_event(conn, db_run, f"ingest failed: {exc}",
                     stage="hiringcafe", source="hiringcafe", level="error")
        db.finish_run(conn, db_run, status="error", error=str(exc))
        return IngestResult(items=len(records), error=str(exc))

    db.log_event(conn, db_run, result.summary(), stage="hiringcafe",
                 source="hiringcafe",
                 level="error" if result.error else "info",
                 counters=result.as_dict())
    db.finish_run(conn, db_run, status="error" if result.error else "ok",
                  stats_payload=result.as_dict(), error=result.error)
    log.info("hiringcafe: %s", result.summary())
    return result
