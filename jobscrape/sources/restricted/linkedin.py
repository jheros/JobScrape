"""LinkedIn public job-guest endpoint. OPT-IN ONLY.

Scraping LinkedIn violates their Terms of Service and their anti-bot systems
block this endpoint frequently. Enabled only via ``--include-restricted``.

Uses the unauthenticated guest API that backs their public job search. Kept
deliberately slow and shallow.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from ...http import Fetcher
from ...models import Job
from ..base import DEFAULT_QUERIES, GlobalSource

log = logging.getLogger("jobscrape.linkedin")

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
PAGE_SIZE = 25
MAX_PAGES = 8

_CARD_RE = re.compile(r"<li>(.*?)</li>", re.S)
_JOB_ID_RE = re.compile(r"(?:currentJobId=|/jobs/view/[^\"']*?-)(\d{8,})")
_TITLE_RE = re.compile(r'class="base-search-card__title"[^>]*>(.*?)<', re.S)
_COMPANY_RE = re.compile(r'class="hidden-nested-link"[^>]*>(.*?)<', re.S)
_LOCATION_RE = re.compile(r'class="job-search-card__location"[^>]*>(.*?)<', re.S)
_DATE_RE = re.compile(r'datetime="([\d-]+)"')
_URL_RE = re.compile(r'href="(https://www\.linkedin\.com/jobs/view/[^"?]+)')


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text)).strip()


class LinkedIn(GlobalSource):
    name = "linkedin"
    note = "OPT-IN. Scraping violates LinkedIn ToS; blocks often. Unreliable."

    async def fetch(
        self,
        fetcher: Fetcher,
        since: date | None = None,
        until: date | None = None,
        queries: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        blocked = 0

        for term in queries or DEFAULT_QUERIES[:6]:
            for page in range(MAX_PAGES):
                params: dict[str, Any] = {
                    "keywords": term,
                    "location": "United States",
                    "start": page * PAGE_SIZE,
                    "f_TPR": self._recency(since),
                }
                html = await fetcher.get_text(
                    SEARCH,
                    params={k: v for k, v in params.items() if v},
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0 Safari/537.36"
                        ),
                        "Accept": "text/html,application/xhtml+xml",
                    },
                    retries=1,
                )
                if not html:
                    blocked += 1
                    break
                cards = _CARD_RE.findall(html)
                if not cards:
                    break
                for card in cards:
                    job = self._parse(card)
                    if job and job.source_job_id not in seen:
                        seen.add(job.source_job_id)
                        jobs.append(job)
                if max_results and len(jobs) >= max_results:
                    return jobs

        if blocked and not jobs:
            log.warning(
                "LinkedIn returned nothing (%d blocked responses) — expected; "
                "this source is unreliable by nature.",
                blocked,
            )
        return jobs

    @staticmethod
    def _recency(since: date | None) -> str | None:
        """Map the window onto LinkedIn's coarse recency filter."""
        if not since:
            return None
        days = (date.today() - since).days
        if days <= 1:
            return "r86400"
        if days <= 7:
            return "r604800"
        if days <= 30:
            return "r2592000"
        return None

    def _parse(self, card: str) -> Job | None:
        id_match = _JOB_ID_RE.search(card)
        url_match = _URL_RE.search(card)
        if not id_match and not url_match:
            return None
        job_id = id_match.group(1) if id_match else url_match.group(1)
        title = _clean(_TITLE_RE.search(card).group(1) if _TITLE_RE.search(card) else "")
        if not title:
            return None
        company = _clean(
            _COMPANY_RE.search(card).group(1) if _COMPANY_RE.search(card) else ""
        )
        location = _clean(
            _LOCATION_RE.search(card).group(1) if _LOCATION_RE.search(card) else ""
        )
        posted = _DATE_RE.search(card)
        url = (
            url_match.group(1)
            if url_match
            else f"https://www.linkedin.com/jobs/view/{job_id}"
        )
        return Job(
            source=self.name,
            source_job_id=str(job_id),
            title=title,
            company=company,
            job_url=url,
            apply_url=url,
            location_raw=location or None,
            posted_at=posted.group(1) if posted else None,
            raw={"note": "listing card only; description not fetched"},
        )
