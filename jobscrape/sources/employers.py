"""Direct employer career sites.

Uber runs its own careers API rather than an ATS we already cover, so its
openings are invisible to every other adapter here. It is a
:class:`GlobalSource` rather than a :class:`BoardSource` because there is no
slug to discover — a single, fixed employer whose whole catalogue is
enumerable, which is also what makes source-wide closure correct for it.

**Amazon and Apple used to live here too**, with hand-written adapters for
amazon.jobs and jobs.apple.com. Both are on `filters.BLOCKED_COMPANIES` now, so
those adapters existed only to fetch postings the filter would immediately
discard — roughly 4,700 requests a sweep between them, against a 10,000-result
cap and a 400-page one. Deleted rather than disabled; `git log` has them if
either is ever unblocked.

**Meta and Tesla are deliberately absent.** Both need a stealth headless browser,
and Tesla additionally a residential proxy — ats-scrapers records httpx,
httpcloak, curl_cffi *and* Browserbase all failing against Tesla's Akamai. That
is a different class of dependency, and an adapter that cannot run is worse than
an honest gap.

Credit: endpoint shapes cross-checked against ats-scrapers (MIT) —
https://github.com/kalil0321/ats-scrapers
"""

from __future__ import annotations

import html
import logging
from datetime import date, datetime, timezone
from typing import Any

from ..http import Fetcher
from ..models import Job, html_to_text
from .base import GlobalSource, JobBatch

log = logging.getLogger("jobscrape.employers")

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _unescape(value: Any) -> str | None:
    """Several of these APIs return HTML entities in plain-text fields."""
    if not isinstance(value, str) or not value.strip():
        return None
    return html.unescape(value).strip()


#: A requisition listed at many sites can accumulate a very long location
#: string; this keeps the column readable while recording how many were folded
#: in, so a truncated list never reads as a complete one.
# =========================================================================
# Uber
# =========================================================================

UBER_SEARCH = "https://www.uber.com/api/loadSearchJobsResults"
UBER_PAGE = 100
UBER_MAX_PAGES = 100


class Uber(GlobalSource):
    """uber.com careers RPC.

    ``limit`` and ``page`` must be **top-level**, not inside ``params``. Nested,
    the API still answers 200 and still reports the right total — it just
    returns the identical full result set for every page. Measured: nested,
    pages 0 and 1 overlap 279 of 279; top-level, they overlap 0 of 10. A reader
    that gets this wrong sees plausible data forever and silently collects one
    page's worth.

    **These postings carry no description.** The field exists in the response
    and is always empty; there is no detail RPC (four candidate names all return
    ``ERR_MISSING_HANDLER``) and the public job page answers 406. So Uber rows
    are title/department/location/date only, and downstream role filtering has
    just the title to work from.
    """

    name = "uber"
    note = "Careers RPC; ~280 US postings, titles and locations but no descriptions."

    async def fetch(
        self,
        fetcher: Fetcher,
        since: date | None = None,
        until: date | None = None,
        queries: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[Job]:
        seen: dict[str, Job] = {}
        complete = True
        for page in range(UBER_MAX_PAGES):
            data = await fetcher.post_json(
                UBER_SEARCH,
                # limit/page top-level — see the class docstring.
                {"limit": UBER_PAGE, "page": page,
                 "params": {"location": [{"country": "USA"}]}},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    # The endpoint requires the header to be present but does
                    # not validate it; no cookie session is needed.
                    "x-csrf-token": "x",
                    "User-Agent": _BROWSER_UA,
                },
            )
            payload = (data or {}).get("data") if isinstance(data, dict) else None
            if not isinstance(payload, dict):
                complete = False
                break
            rows = payload.get("results") or []
            if not rows:
                break
            before = len(seen)
            for row in rows:
                job = self._parse(row)
                if job is not None:
                    seen.setdefault(job.source_job_id, job)
            if len(seen) == before:
                # Every row was a duplicate: the pagination is not advancing.
                # Stopping here is what keeps the nested-params trap from
                # becoming an endless loop if the payload shape ever changes.
                break
            if len(rows) < UBER_PAGE or len(seen) >= _uber_total(payload):
                break
            if max_results and len(seen) >= max_results:
                complete = False
                break
        else:
            complete = False

        jobs = list(seen.values())
        return JobBatch(jobs, complete=complete and bool(jobs))

    def _parse(self, row: dict[str, Any]) -> Job | None:
        # `or ""` would discard a numeric id of 0, which is a valid identifier
        # and falsy. Check for absence explicitly.
        raw_id = row.get("id")
        job_id = "" if raw_id is None else str(raw_id).strip()
        if not job_id:
            return None
        place = row.get("location") if isinstance(row.get("location"), dict) else {}
        everywhere = [p for p in (row.get("allLocations") or []) if isinstance(p, dict)]
        labels = [
            ", ".join(str(p[k]) for k in ("city", "region", "countryName")
                      if p.get(k))
            for p in (everywhere or [place]) if p
        ]
        return Job(
            source=self.name,
            source_job_id=job_id,
            title=_unescape(row.get("title")) or "",
            company="Uber",
            company_slug="uber",
            job_url=f"https://www.uber.com/global/en/careers/list/{job_id}/",
            apply_url=f"https://www.uber.com/global/en/careers/list/{job_id}/",
            location_raw="; ".join(dict.fromkeys(l for l in labels if l)) or None,
            city=place.get("city"),
            state=place.get("region"),
            country=place.get("country"),
            employment_type=_unescape(row.get("timeType")),
            department=_unescape(row.get("department")),
            posted_at=row.get("creationDate"),
            updated_at=row.get("updatedDate"),
            raw=row,
        )


def _uber_total(payload: dict[str, Any]) -> int:
    """``totalResults`` is a Long object, not a number.

    It arrives as ``{"low": 279, "high": 0, "unsigned": false}``. Read naively
    with ``int()`` it raises, and defaulting to 0 on the exception would end
    pagination after the first page.
    """
    total = payload.get("totalResults")
    if isinstance(total, dict):
        try:
            return int(total.get("low") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(total or 0)
    except (TypeError, ValueError):
        return 0
