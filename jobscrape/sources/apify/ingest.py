"""Turn an Apify run into stored postings.

The one path both entry points share: the webhook receiver calls it when Apify
says a run finished, and ``jobscrape apify-pull`` calls it to re-ingest a run by
hand. Keeping it in one place is what makes the catch-up command a genuine
recovery route rather than a second implementation that might drift.

Downstream of the mapping, nothing here is new: `apply_filters_counted` decides
what to keep and `db.upsert_jobs` writes it, exactly as the fetch loop does for
every other source. Apify postings therefore get the same role filter, the same
US-eligibility test, the same workplace classification, seniority ladder and
skill extraction as a Greenhouse posting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ... import db
from ...filters import apply_filters_counted
from ...http import Fetcher
from . import client, resolve_mapper

log = logging.getLogger("jobscrape.apify")


@dataclass
class IngestResult:
    """What one run produced, in the shape the CLI and the log both want."""

    run_id: str
    actor: str = ""
    dataset_id: str = ""
    items: int = 0
    mapped: int = 0
    kept: int = 0
    inserted: int = 0
    updated: int = 0
    #: Board slug recovered from the apply URL, per :mod:`.ats_link`. Reported
    #: because cross-source dedup depends on it, and the rate is the only way
    #: to know whether that assumption holds against real data.
    ats_linked: int = 0
    funnel: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "actor": self.actor,
            "dataset_id": self.dataset_id, "items": self.items,
            "mapped": self.mapped, "kept": self.kept,
            "inserted": self.inserted, "updated": self.updated,
            "ats_linked": self.ats_linked, "funnel": self.funnel,
            "error": self.error,
        }

    def summary(self) -> str:
        if self.error:
            return f"run {self.run_id}: {self.error}"
        linked = (f", {self.ats_linked}/{self.mapped} linked to an ATS board"
                  if self.mapped else "")
        return (
            f"run {self.run_id} [{self.actor or '?'}]: {self.items} items, "
            f"{self.mapped} mapped, {self.kept} kept, "
            f"{self.inserted} new, {self.updated} refreshed{linked}"
        )


async def ingest_run(
    fetcher: Fetcher,
    conn: db.Connection,
    run_id: str,
    *,
    max_items: int | None = None,
    allow_roles: tuple[str, ...] = (),
    software_only: bool = True,
    us_only: bool = True,
) -> IngestResult:
    """Fetch one run's dataset, map it, filter it and store it.

    The run is read back from the API rather than trusted from a caller. That
    matters for the webhook: its payload is unauthenticated input carrying both
    a run id and a dataset id, and Apify offers no request signing, so a forged
    body could otherwise name any dataset on the account. Resolving the dataset
    id *from the run* means only datasets that run actually produced can be
    ingested, and the run's real status is checked rather than taken on trust.
    """
    result = IngestResult(run_id=run_id)

    info = await client.run_info(fetcher, run_id)
    if not info:
        result.error = "run could not be read from the Apify API"
        return result

    status = str(info.get("status") or "")
    if status != "SUCCEEDED":
        result.error = f"run status is {status or 'unknown'}, not SUCCEEDED"
        return result

    result.actor = str(info.get("actId") or info.get("actorId") or "")
    result.dataset_id = str(info.get("defaultDatasetId") or "")
    if not result.dataset_id:
        result.error = "run carries no defaultDatasetId"
        return result

    # `resolve_mapper`, not `mapper_for`: a run's `actId` is an opaque id
    # (`nlZZi3lZre4fM9IET`), and matching it against the substrings "indeed" and
    # "linkedin" returned None for every real run — so this branch fired every
    # time and nothing was ever ingested.
    mapper = await resolve_mapper(fetcher, result.actor)
    if mapper is None:
        result.error = (
            f"no mapper for actor {result.actor!r} — configure "
            f"APIFY_INDEED_ACTOR / APIFY_LINKEDIN_ACTOR, or check the actor is public"
        )
        return result

    items = await client.dataset_items(fetcher, result.dataset_id, max_items=max_items)
    result.items = len(items)
    if not items:
        return result

    jobs = mapper.to_jobs(items)
    result.mapped = len(jobs)
    result.ats_linked = sum(
        1 for job in jobs
        if job.company_slug and job.raw
        and _looks_like_board_slug(job)
    )

    kept, funnel = apply_filters_counted(
        jobs, software_only=software_only, us_only=us_only, allow_roles=allow_roles
    )
    result.kept = len(kept)
    result.funnel = funnel.as_dict()
    if kept:
        result.inserted, result.updated = db.upsert_jobs(conn, kept)
    return result


def _looks_like_board_slug(job: Any) -> bool:
    """Whether this row's slug came from an apply URL rather than the name.

    Recomputed rather than flagged on the `Job`, so the dataclass stays a
    description of a posting instead of carrying provenance for one report.
    """
    from . import ats_link

    apply_url = getattr(job, "apply_url", None)
    job_url = getattr(job, "job_url", None)
    board = ats_link.board_slug(apply_url) or ats_link.board_slug(job_url)
    return board is not None


async def ingest_and_record(
    conn: db.Connection,
    run_id: str,
    *,
    command: str = "apify-webhook",
    **kwargs: Any,
) -> IngestResult:
    """`ingest_run` wrapped in the project's run bookkeeping.

    Recorded in `runs`/`run_events` so an Apify ingestion appears on the
    dashboard's Runs page beside every other stage — otherwise this would be
    the one way postings enter the archive with no trace of how they got there.
    """
    db_run = db.start_run(conn, command=command, stage="apify")
    try:
        async with Fetcher() as fetcher:
            result = await ingest_run(fetcher, conn, run_id, **kwargs)
    except Exception as exc:                      # noqa: BLE001 - reported, not raised
        log.exception("apify: ingesting run %s failed", run_id)
        db.log_event(conn, db_run, f"run {run_id} failed: {exc}",
                     stage="apify", source="apify", level="error")
        db.finish_run(conn, db_run, status="error", error=str(exc))
        return IngestResult(run_id=run_id, error=str(exc))

    db.log_event(conn, db_run, result.summary(), stage="apify",
                 source=result.actor or "apify",
                 level="error" if result.error else "info",
                 counters=result.as_dict())
    db.finish_run(conn, db_run, status="error" if result.error else "ok",
                  stats_payload=result.as_dict(), error=result.error)
    log.info("apify: %s", result.summary())
    return result
