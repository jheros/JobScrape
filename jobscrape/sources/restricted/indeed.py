"""Indeed. OPT-IN ONLY.

Scraping Indeed violates their Terms of Service, and they run one of the more
aggressive anti-bot stacks on the web (Cloudflare + behavioural challenges).
Plain HTTP requests are blocked almost immediately, so this adapter will
usually return nothing.

The honest, supported path to Indeed data is their official partner Publisher
API — apply at https://www.indeed.com/publisher. If you obtain a key, set
``INDEED_PUBLISHER_ID`` and this adapter uses it instead of scraping.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Any

from ...http import Fetcher
from ...models import Job
from ..base import DEFAULT_QUERIES, GlobalSource

log = logging.getLogger("jobscrape.indeed")

PUBLISHER_API = "https://api.indeed.com/ads/apisearch"
HTML_SEARCH = "https://www.indeed.com/jobs"

_MOSAIC_RE = re.compile(r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*(\{.*?\});', re.S)


class Indeed(GlobalSource):
    name = "indeed"
    note = "OPT-IN. Scraping violates Indeed ToS and is usually blocked."

    def available(self) -> tuple[bool, str]:
        return True, ""

    async def fetch(
        self,
        fetcher: Fetcher,
        since: date | None = None,
        until: date | None = None,
        queries: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[Job]:
        publisher_id = os.environ.get("INDEED_PUBLISHER_ID", "")
        if publisher_id:
            return await self._via_publisher_api(
                fetcher, publisher_id, queries, since, max_results
            )
        return await self._via_html(fetcher, queries, max_results)

    async def _via_publisher_api(
        self,
        fetcher: Fetcher,
        publisher_id: str,
        queries: list[str] | None,
        since: date | None,
        max_results: int | None,
    ) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        for term in queries or DEFAULT_QUERIES:
            for start in range(0, 200, 25):
                data = await fetcher.get_json(
                    PUBLISHER_API,
                    params={
                        "publisher": publisher_id,
                        "q": term,
                        "l": "United States",
                        "sort": "date",
                        "limit": 25,
                        "start": start,
                        "format": "json",
                        "v": "2",
                        "fromage": max((date.today() - since).days, 1) if since else "",
                    },
                )
                if not isinstance(data, dict):
                    break
                results = data.get("results") or []
                if not results:
                    break
                for item in results:
                    key = str(item.get("jobkey") or "")
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    jobs.append(
                        Job(
                            source=self.name,
                            source_job_id=key,
                            title=item.get("jobtitle") or "",
                            company=item.get("company") or "",
                            job_url=item.get("url") or "",
                            apply_url=item.get("url"),
                            location_raw=item.get("formattedLocation"),
                            city=item.get("city"),
                            state=item.get("state"),
                            country=item.get("country"),
                            posted_at=item.get("date"),
                            description_text=item.get("snippet") or None,
                            raw=item,
                        )
                    )
                if max_results and len(jobs) >= max_results:
                    return jobs
        return jobs

    async def _via_html(
        self, fetcher: Fetcher, queries: list[str] | None, max_results: int | None
    ) -> list[Job]:
        jobs: list[Job] = []
        blocked = 0
        for term in (queries or DEFAULT_QUERIES)[:4]:
            html = await fetcher.get_text(
                HTML_SEARCH,
                params={"q": term, "l": "United States", "sort": "date"},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml",
                },
                retries=0,
            )
            if not html or "mosaic-provider-jobcards" not in html:
                blocked += 1
                continue
            jobs.extend(self._parse_mosaic(html))
            if max_results and len(jobs) >= max_results:
                break

        if blocked:
            log.warning(
                "Indeed blocked %d request(s) and returned no usable data. This is "
                "the expected outcome; set INDEED_PUBLISHER_ID to use their official API.",
                blocked,
            )
        return jobs

    def _parse_mosaic(self, html: str) -> list[Job]:
        """Extract the embedded job-card JSON blob, when it survives."""
        import json

        match = _MOSAIC_RE.search(html)
        if not match:
            return []
        try:
            blob = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []
        results = (
            blob.get("metaData", {})
            .get("mosaicProviderJobCardsModel", {})
            .get("results", [])
        )
        jobs: list[Job] = []
        for item in results:
            key = str(item.get("jobkey") or "")
            if not key:
                continue
            jobs.append(
                Job(
                    source=self.name,
                    source_job_id=key,
                    title=item.get("displayTitle") or item.get("title") or "",
                    company=item.get("company") or "",
                    job_url=f"https://www.indeed.com/viewjob?jk={key}",
                    apply_url=f"https://www.indeed.com/viewjob?jk={key}",
                    location_raw=item.get("formattedLocation"),
                    posted_at=item.get("pubDate"),
                    description_text=" ".join(item.get("jobDescriptionSnippet") or [])
                    if isinstance(item.get("jobDescriptionSnippet"), list)
                    else item.get("jobDescriptionSnippet"),
                    raw=item,
                )
            )
        return jobs
