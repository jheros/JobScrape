"""Workday career sites via the public CXS API.

Workday hosts the large US enterprises, so it matters for coverage — but it is
the most expensive source here. The list endpoint reports dates only as
relative prose (``"Posted 30+ Days Ago"``), which is useless for a date-window
filter, so an exact ``startDate`` requires one detail request per posting.

A company is identified by a ``tenant|wd-number|site`` triple, e.g.
``nvidia|wd5|NVIDIAExternalCareerSite``.
"""

from __future__ import annotations

import re
from typing import Any

from .. import config
from ..http import Fetcher, gather_limited
from ..models import Job, html_to_text
from .base import BoardSource, DEFAULT_QUERIES, Prefilter, shallow_refresh

CXS = "https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
PAGE_SIZE = 20
#: Cap when falling back to per-keyword search (see `fetch_board`).
MAX_PER_QUERY = 200


def parse_slug(slug: str) -> tuple[str, str, str] | None:
    """Split a ``tenant|wd|site`` triple, tolerating a bare tenant name."""
    parts = slug.split("|")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 1 and parts[0]:
        # Best-effort default; validation will discard it if wrong.
        return parts[0], "wd1", "External"
    return None


class Workday(BoardSource):
    name = "workday"
    ats = "workday"
    note = "Public CXS API; needs a detail call per job for the exact date."

    async def _search(
        self, fetcher: Fetcher, base: str, query: str, limit: int
    ) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        offset = 0
        total = None
        while offset < limit:
            data = await fetcher.post_json(
                f"{base}/jobs",
                {
                    "appliedFacets": {},
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "searchText": query,
                },
                headers={"content-type": "application/json", "accept": "application/json"},
            )
            if not isinstance(data, dict):
                break
            batch = data.get("jobPostings") or []
            if not batch:
                break
            found.extend(b for b in batch if isinstance(b, dict))
            # Workday reports `total` only on the first page; later pages send
            # 0, so latch the real value instead of re-reading it each time.
            if total is None:
                total = int(data.get("total") or 0)
            offset += PAGE_SIZE
            # Stopping at `total` is required, not just an optimisation:
            # Workday keeps serving full pages past the end of the result set
            # (offset=2000 on a 2,000-posting board returns 20 more rows), so
            # waiting for a short page to signal the end would never terminate.
            if len(batch) < PAGE_SIZE or (total and offset >= total):
                break
        return found

    async def _detail(self, fetcher: Fetcher, base: str, path: str) -> dict[str, Any] | None:
        data = await fetcher.get_json(f"{base}{path}", headers={"accept": "application/json"})
        if isinstance(data, dict):
            return data.get("jobPostingInfo") or None
        return None

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        known_ids: set[str] | None = None,
        queries: list[str] | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        parsed = parse_slug(slug)
        if not parsed:
            return None
        tenant, wd, site = parsed
        base = CXS.format(tenant=tenant, wd=wd, site=site)
        site_url = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"

        seen: dict[str, dict[str, Any]] = {}

        # An EMPTY search enumerates the whole board, and beats keyword search
        # on every tenant measured — AIG 475 postings vs 28, 3M 655 vs 41,
        # NVIDIA 2,000 vs 1,694. Keyword search also can't be made complete by
        # adding terms: it misses anything whose title doesn't happen to match,
        # so it silently under-collects rather than just running slower.
        if queries is None:
            limit = config.WORKDAY_MAX_PER_BOARD if with_details else PAGE_SIZE
            for posting in await self._search(fetcher, base, "", limit):
                path = posting.get("externalPath")
                if path:
                    seen[path] = posting

        # Fall back to keyword search only if the empty search found nothing —
        # a tenant that rejects it, or an explicit `queries` override.
        if not seen:
            terms = queries or (DEFAULT_QUERIES[:4] if with_details else ["software engineer"])
            for term in terms:
                for posting in await self._search(
                    fetcher, base, term, MAX_PER_QUERY if with_details else PAGE_SIZE
                ):
                    path = posting.get("externalPath")
                    if path:
                        seen[path] = posting
                if not with_details and seen:
                    break  # probing only needs to prove the board responds

        if not seen:
            return None if not with_details else []

        # Workday's keyword search is fuzzy and returns plenty of unrelated
        # roles. Dropping them here avoids one detail request each.
        if prefilter is not None:
            seen = {
                path: posting
                for path, posting in seen.items()
                if prefilter(posting.get("title"), None, posting.get("locationsText"))
            }
            if not seen:
                return []

        def _job_id(path: str, posting: dict[str, Any]) -> str:
            # bulletFields carries the requisition id on the *listing*, so the
            # stored id can be reconstructed without a detail request.
            bullets = posting.get("bulletFields") or []
            req = bullets[0] if bullets else re.sub(r".*_", "", path)
            return f"{tenant}:{req or path}"

        known = known_ids or set()

        details: dict[str, dict[str, Any]] = {}
        if with_details:
            paths = [p for p, post in seen.items() if _job_id(p, post) not in known]
            fetched = await gather_limited(
                [self._detail(fetcher, base, p) for p in paths], limit=4
            )
            for path, result in zip(paths, fetched):
                if isinstance(result, dict):
                    details[path] = result

        jobs: list[Job] = []
        for path, posting in seen.items():
            detail = details.get(path, {})
            bullets = posting.get("bulletFields") or []
            req_id = bullets[0] if bullets else re.sub(r".*_", "", path)
            country = (detail.get("country") or {}).get("descriptor")
            location = detail.get("location") or posting.get("locationsText")
            remote_type = detail.get("remoteType")

            job = Job(
                    source=self.name,
                    source_job_id=f"{tenant}:{req_id or path}",
                    title=posting.get("title") or detail.get("title") or "",
                    company=tenant,
                    company_slug=slug,
                    job_url=detail.get("externalUrl") or f"{site_url}{path}",
                    apply_url=f"{site_url}{path}/apply",
                    location_raw=location,
                    country=country,
                    is_remote="remote" in str(remote_type or "").lower() or None,
                    workplace_hint=remote_type,
                    employment_type=detail.get("timeType"),
                    posted_at=detail.get("startDate"),
                    description_html=detail.get("jobDescription"),
                    description_text=html_to_text(detail.get("jobDescription")) or None,
                    raw=detail if with_details else {},
            )
            if _job_id(path, posting) in known:
                job = shallow_refresh(job)
            jobs.append(job)
        return jobs

    async def probe(self, fetcher: Fetcher, slug: str) -> tuple[str, int]:
        jobs = await self.fetch_board(fetcher, slug, with_details=False)
        if jobs is None:
            return "dead", 0
        return ("live", len(jobs)) if jobs else ("empty", 0)
