"""Probe discovered slugs to separate real boards from index junk."""

from __future__ import annotations

import asyncio
import logging

from .. import db
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
) -> dict[str, int]:
    """Probe every unchecked slug for one ATS, recording live/empty/dead.

    Results are committed in batches so an interrupted run keeps its progress.
    """
    # Restricted boards are discovered like any other, so they can be validated
    # on request even though a normal fetch skips them.
    source = BOARD_SOURCES.get(ats) or restricted_board_sources().get(ats)
    if source is None:
        return {}

    statuses = None if recheck else "unchecked"
    rows = db.get_companies(conn, ats=ats, status=statuses, limit=limit)
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
            except Exception as exc:  # noqa: BLE001 - one bad board must not stop the sweep
                log.debug("probe %s/%s failed: %s", ats, slug, exc)
                status, count = "dead", 0
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
    log.info(
        "%s: %d probed -> live=%d empty=%d dead=%d",
        ats, len(rows), counts["live"], counts["empty"], counts["dead"],
    )
    return counts
