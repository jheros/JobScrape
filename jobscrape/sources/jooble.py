"""Jooble aggregator API.

Free key from https://jooble.org/api/about. POST-based; the key is part of the
URL path rather than a header.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from .. import config
from ..http import Fetcher
from ..models import Job
from .base import DEFAULT_QUERIES, GlobalSource, JobBatch

log = logging.getLogger("jobscrape.jooble")

API = "https://jooble.org/api/{key}"
#: Verified to actually bind: Jooble reports ~196,000 matches for a single
#: term, and the previous 10-page limit saw 200 of them. Tunable via
#: JOOBLE_MAX_PAGES; this runs once per search term, so raising it multiplies
#: request count across the whole term list.
MAX_PAGES = config.JOOBLE_MAX_PAGES

_SALARY_RE = re.compile(r"([\d,]+)\s*(?:-|–|to)\s*([\d,]+)")


class Jooble(GlobalSource):
    name = "jooble"
    requires_key = True
    note = "Aggregator breadth. Needs a free JOOBLE_API_KEY."

    def available(self) -> tuple[bool, str]:
        if not config.JOOBLE_API_KEY:
            return False, "set JOOBLE_API_KEY (free: jooble.org/api/about)"
        return True, ""

    async def fetch(
        self,
        fetcher: Fetcher,
        since: date | None = None,
        until: date | None = None,
        queries: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[Job]:
        ok, _ = self.available()
        if not ok:
            return JobBatch()

        url = API.format(key=config.JOOBLE_API_KEY)
        jobs: list[Job] = []
        seen: set[str] = set()
        # See JobBatch — only an exhaustive crawl may be used to close postings.
        complete = True
        for term in queries or DEFAULT_QUERIES:
            exhausted = False
            for page in range(1, MAX_PAGES + 1):
                data = await fetcher.post_json(
                    url,
                    {"keywords": term, "location": "United States", "page": str(page)},
                    headers={"content-type": "application/json"},
                )
                if not isinstance(data, dict):
                    # Logged, not swallowed. This is the shape a quota refusal
                    # arrives in, and the page budget was raised from 30 to 60
                    # against a term list that also grew — so "Jooble stopped
                    # early" needs to be distinguishable from "Jooble ran out of
                    # jobs" without reading the funnel afterwards.
                    log.warning(
                        "jooble: %r page %d returned %s, not an object — stopping "
                        "this term (quota, or a transient failure)",
                        term, page, type(data).__name__,
                    )
                    complete = False  # the page failed; the rest is unread
                    break
                results = data.get("jobs") or []
                if not results:
                    exhausted = True
                    break
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    job_id = str(item.get("id") or item.get("link") or "")
                    if not job_id or job_id in seen:
                        continue
                    seen.add(job_id)
                    lo = hi = None
                    match = _SALARY_RE.search(str(item.get("salary") or ""))
                    if match:
                        try:
                            lo = float(match.group(1).replace(",", ""))
                            hi = float(match.group(2).replace(",", ""))
                        except ValueError:
                            lo = hi = None
                    jobs.append(
                        Job(
                            source=self.name,
                            source_job_id=job_id,
                            title=item.get("title") or "",
                            company=item.get("company") or "",
                            job_url=item.get("link") or "",
                            apply_url=item.get("link"),
                            location_raw=item.get("location"),
                            country="US",
                            employment_type=item.get("type"),
                            posted_at=item.get("updated"),
                            salary_min=lo,
                            salary_max=hi,
                            salary_raw=item.get("salary") or None,
                            description_html=item.get("snippet") or None,
                            raw=item,
                        )
                    )
            else:
                # Full page budget spent without running out — Jooble reports
                # ~196,000 matches for a single term, so this is the norm.
                complete = False
            if not exhausted:
                complete = False
            if max_results and len(jobs) >= max_results:
                complete = False  # capped, not exhausted
                break
        return JobBatch(jobs, complete=complete)
