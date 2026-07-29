"""Avature enterprise career sites. OPT-IN ONLY.

Verified working with plain HTTP: the search page links each posting and the
posting pages carry schema.org ``JobPosting`` data (title, date, description).

Lives in the restricted tier for two reasons, not because it is broken:

* These are enterprise career portals whose terms generally prohibit automated
  collection, unlike the public ATS board APIs.
* The search page is paginated per tenant with no documented contract, so it is
  markedly more fragile than the JSON sources.

Its schema.org blocks tend to omit ``jobLocation``, so location is often absent
and the permissive location filter is what keeps those rows.
"""

from __future__ import annotations

import re
from typing import Any

from ...http import Fetcher, gather_limited
from ...models import Job
from .. import jsonld
from ..base import BoardSource, Prefilter, shallow_refresh

SEARCH = "https://{slug}.avature.net/careers/SearchJobs"
BASE = "https://{slug}.avature.net"

_DETAIL_RE = re.compile(r'href="([^"]*?/careers/JobDetail/[^"?#]+)"', re.I)
_ID_RE = re.compile(r"/JobDetail/(?:[^/]+/)?(\d+)")
MAX_PAGES = 5


class Avature(BoardSource):
    name = "avature"
    ats = "avature"
    note = "OPT-IN. Enterprise portal scraping; fragile and ToS-sensitive."

    async def _links(self, fetcher: Fetcher, slug: str) -> list[str]:
        base = BASE.format(slug=slug)
        found: list[str] = []
        seen: set[str] = set()
        for page in range(MAX_PAGES):
            url = SEARCH.format(slug=slug)
            if page:
                url = f"{url}?jobOffset={page * 10}"
            html = await fetcher.get_text(url, retries=1)
            if not html:
                break
            new = 0
            for href in _DETAIL_RE.findall(html):
                full = href if href.startswith("http") else base + href
                if full not in seen:
                    seen.add(full)
                    found.append(full)
                    new += 1
            if not new:
                break
        return found

    async def _posting(self, fetcher: Fetcher, url: str) -> tuple[str, dict[str, Any] | None]:
        html = await fetcher.get_text(url, retries=1)
        return url, jsonld.first_job_posting(html)

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        known_ids: set[str] | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        links = await self._links(fetcher, slug)
        if not links:
            return None

        # The URL carries a human-readable title slug, enough to prefilter on.
        def _title_hint(url: str) -> str:
            match = re.search(r"/JobDetail/([^/]+)/", url)
            return match.group(1).replace("-", " ") if match else ""

        candidates = [
            url for url in links
            if prefilter is None or prefilter(_title_hint(url), None, None)
        ]
        if not candidates:
            return []

        if not with_details:
            return [
                Job(
                    source=self.name,
                    source_job_id=f"{slug}:{(_ID_RE.search(u) or [None, u])[1]}",
                    title=_title_hint(u),
                    company=slug,
                    company_slug=slug,
                    job_url=u,
                )
                for u in candidates
            ]

        def _job_id(url: str) -> str:
            match = _ID_RE.search(url)
            return f"{slug}:{match.group(1) if match else url.rsplit('/', 1)[-1]}"

        known = known_ids or set()
        fresh = [u for u in candidates if _job_id(u) not in known]

        jobs: list[Job] = [
            shallow_refresh(Job(
                source=self.name,
                source_job_id=_job_id(u),
                title=_title_hint(u),
                company=slug,
                company_slug=slug,
                job_url=u,
                apply_url=u,
            ))
            for u in candidates if _job_id(u) in known
        ]

        results = await gather_limited(
            [self._posting(fetcher, u) for u in fresh], limit=3
        )

        for result in results:
            if not result:
                continue
            url, posting = result
            if not isinstance(posting, dict):
                continue
            match = _ID_RE.search(url)
            job_id = match.group(1) if match else url.rsplit("/", 1)[-1]
            low, high, currency = jsonld.salary(posting)
            jobs.append(
                Job(
                    source=self.name,
                    source_job_id=f"{slug}:{job_id}",
                    title=posting.get("title") or _title_hint(url),
                    company=jsonld.hiring_org(posting) or slug,
                    company_slug=slug,
                    job_url=url,
                    apply_url=url,
                    location_raw=jsonld.location_text(posting),
                    is_remote=jsonld.is_remote(posting),
                    workplace_hint="remote" if jsonld.is_remote(posting) else None,
                    employment_type=jsonld.employment_type(posting),
                    posted_at=posting.get("datePosted"),
                    salary_min=low,
                    salary_max=high,
                    salary_currency=currency,
                    description_html=jsonld.description(posting),
                    raw=posting,
                )
            )
        return jobs
