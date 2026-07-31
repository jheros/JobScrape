"""JazzHR public board (``{slug}.applytojob.com``).

No JSON API, but the listing page links every posting and each posting page
carries a complete schema.org ``JobPosting`` block — title, date, location,
employment type and the full description. That makes it as reliable as the JSON
sources despite being HTML.
"""

from __future__ import annotations

import re
from typing import Any

from ..http import Fetcher, gather_limited
from ..models import Job
from . import jsonld
from .base import BoardSource, Prefilter, shallow_refresh

LIST = "https://{slug}.applytojob.com/apply/"

# Posting URLs look like /apply/<token>/<Title-Slug>; the token is the id.
_JOB_LINK_RE = re.compile(
    r'href="(https://[a-z0-9-]+\.applytojob\.com/apply/([A-Za-z0-9]{6,})/[^"?#]+)"',
    re.I,
)


class JazzHR(BoardSource):
    name = "jazzhr"
    ats = "jazzhr"
    note = "HTML board; each posting page carries schema.org JobPosting data."

    async def _posting(self, fetcher: Fetcher, url: str) -> dict[str, Any] | None:
        html = await fetcher.get_text(url, retries=1)
        return jsonld.first_job_posting(html)

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        known_ids: set[str] | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        html = await fetcher.get_text(LIST.format(slug=slug), retries=1)
        if not html:
            return None
        # A board that exists but lists nothing still renders its apply page.
        if "applytojob.com" not in html:
            return None

        seen: dict[str, str] = {}
        for url, token in _JOB_LINK_RE.findall(html):
            seen.setdefault(token, url)
        if not seen:
            return []

        # The listing gives only a URL-slug title, which is enough for the
        # cheap prefilter and saves a request per obviously-irrelevant posting.
        candidates: dict[str, str] = {}
        for token, url in seen.items():
            title_hint = url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
            if prefilter is None or prefilter(title_hint, None, None):
                candidates[token] = url
        if not candidates:
            return []

        if not with_details:
            # Probing only needs proof the board responds with postings.
            return [
                Job(
                    source=self.name,
                    source_job_id=f"{slug}:{token}",
                    title=url.rstrip("/").rsplit("/", 1)[-1].replace("-", " "),
                    company=slug,
                    company_slug=slug,
                    job_url=url,
                )
                for token, url in candidates.items()
            ]

        # A stored posting needs no page fetch; emit it from listing data so
        # last_seen_at advances and it is not treated as closed.
        known = known_ids or set()
        tokens = [t for t in candidates if f"{slug}:{t}" not in known]
        postings = await gather_limited(
            [self._posting(fetcher, candidates[t]) for t in tokens], limit=4
        )

        jobs: list[Job] = []
        for token in candidates:
            if f"{slug}:{token}" in known:
                jobs.append(shallow_refresh(Job(
                    source=self.name,
                    source_job_id=f"{slug}:{token}",
                    title=candidates[token].rstrip("/").rsplit("/", 1)[-1].replace("-", " "),
                    company=slug,
                    company_slug=slug,
                    job_url=candidates[token],
                    apply_url=candidates[token],
                )))

        for token, posting in zip(tokens, postings, strict=True):
            url = candidates[token]
            if not isinstance(posting, dict):
                continue
            low, high, currency = jsonld.salary(posting)
            jobs.append(
                Job(
                    source=self.name,
                    source_job_id=f"{slug}:{token}",
                    title=posting.get("title") or "",
                    company=jsonld.hiring_org(posting) or slug,
                    company_slug=slug,
                    job_url=url,
                    apply_url=url,
                    location_raw=jsonld.location_text(posting),
                    is_remote=jsonld.is_remote(posting),
                    workplace_hint="remote" if jsonld.is_remote(posting) else None,
                    employment_type=jsonld.employment_type(posting),
                    department=posting.get("occupationalCategory") or None,
                    posted_at=posting.get("datePosted"),
                    salary_min=low,
                    salary_max=high,
                    salary_currency=currency,
                    description_html=jsonld.description(posting),
                    raw=posting,
                )
            )
        return jobs
