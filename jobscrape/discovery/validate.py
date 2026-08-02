"""Probe discovered slugs to separate real boards from index junk."""

from __future__ import annotations

import asyncio
import logging

from .. import db, filters
from ..http import Fetcher
from ..sources import BOARD_SOURCES, restricted_board_sources

log = logging.getLogger("jobscrape.validate")


async def validate_ats(
    conn: db.Connection,
    fetcher: Fetcher,
    ats: str,
    limit: int | None = None,
    concurrency: int = 8,
    recheck: bool = False,
    sample: bool = False,
) -> dict[str, int]:
    """Probe every unchecked slug for one ATS, recording live/empty/dead.

    Results are committed in batches so an interrupted run keeps its progress.

    ``sample=True`` draws the slice at random. With a `--limit`, the default
    ordering is `job_count DESC, slug`, and every unchecked row has
    `job_count = 0` — so a limited run probes the same alphabetical prefix every
    time, and the live-rate it reports describes the letter A rather than the
    platform. That number is what decides whether validating the remaining
    thousands is worth the hours, so it has to be representative.
    """
    # Restricted boards are discovered like any other, so they can be validated
    # on request even though a normal fetch skips them.
    source = BOARD_SOURCES.get(ats) or restricted_board_sources().get(ats)
    if source is None:
        return {}

    statuses = None if recheck else "unchecked"
    rows = db.get_companies(
        conn, ats=ats, status=statuses, limit=limit,
        order="random" if sample else "yield",
    )
    # A board we will never fetch is not worth a probe either, and there are
    # thousands of unvalidated slugs competing for this budget.
    rows = [r for r in rows if not filters.is_blocked_company(r["slug"])]
    if not rows:
        return {}

    counts: dict[str, int] = {"live": 0, "empty": 0, "dead": 0}
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    done = 0

    async def probe(slug: str) -> None:
        nonlocal done
        async with sem:
            try:
                status, count = await source.probe(fetcher, slug)
            except Exception as exc:
                # An exception is NOT a verdict. `probe` does real network and
                # parsing work, so a transient timeout, an unexpected payload
                # shape or an adapter bug all land here — and writing "dead"
                # would turn any of them into a permanent judgement about the
                # board. `dead` is never selected for a fetch again
                # (`_fetch_boards` asks for `status="live"`), so a board retired
                # this way needs `validate --recheck` to come back.
                #
                # Left `unchecked` instead, and logged at warning rather than
                # debug: this was invisible at the default log level.
                log.warning("probe %s/%s errored, leaving unchecked: %s: %s",
                            ats, slug, type(exc).__name__, exc)
                async with lock:
                    counts["errored"] = counts.get("errored", 0) + 1
                    done += 1
                return
        async with lock:
            counts[status] = counts.get(status, 0) + 1
            db.mark_company(conn, ats, slug, status, count)
            done += 1
            if done % 50 == 0:
                conn.commit()
                log.info(
                    "  %s: %d/%d probed (live=%d empty=%d dead=%d)",
                    ats, done, len(rows), counts["live"], counts["empty"], counts["dead"],
                )

    await asyncio.gather(*(probe(row["slug"]) for row in rows))
    conn.commit()
    errored = counts.get("errored", 0)
    log.info(
        "%s: %d probed -> live=%d empty=%d dead=%d%s",
        ats, len(rows), counts["live"], counts["empty"], counts["dead"],
        f" errored={errored} (left unchecked)" if errored else "",
    )
    return counts
