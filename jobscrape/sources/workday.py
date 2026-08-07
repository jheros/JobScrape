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
from ..models import Job, classify_hint, html_to_text
from .base import BoardSource, DEFAULT_QUERIES, Prefilter, shallow_refresh

CXS = "https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
#: Hard cap. Workday rejects a larger `limit` with HTTP 400.
PAGE_SIZE = 20
#: Cap when falling back to per-keyword search (see `fetch_board`).
MAX_PER_QUERY = 200

#: The value Workday reports as `total` when it is really saying "at least this
#: many". It is a reporting ceiling, not a count: measured on NVIDIA, `total`
#: comes back as exactly 2000 while the jobFamilyGroup facet counts sum to
#: 2,583. Paginating past it wraps to page 1, so a board sitting on this number
#: is silently truncated unless the query is narrowed.
REPORTED_TOTAL_CAP = 2000

#: How deep to recurse when narrowing. Each level multiplies request count, and
#: three levels covers the largest tenants observed.
MAX_SUBDIVISION_DEPTH = 3


def parse_slug(slug: str) -> tuple[str, str, str] | None:
    """Split a ``tenant|wd|site`` triple, tolerating a bare tenant name."""
    parts = slug.split("|")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 1 and parts[0]:
        # Best-effort default; validation will discard it if wrong.
        return parts[0], "wd1", "External"
    return None


#: What Workday puts in the listing's `locationsText` when a posting spans
#: several offices — literally "2 Locations". It is a UI summary, not a place,
#: and it used to be the fallback whenever a detail request failed, which stored
#: it as the entire `location_raw` for 1,118 postings and guaranteed
#: city/state/country stayed blank on all of them.
_PLACEHOLDER_LOCATION = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)


def _location(detail: dict[str, Any], posting: dict[str, Any]) -> str | None:
    """The posting's real location, joining the extra offices when it has them.

    `detail.location` is a real place ("Hampton, VA, US") and
    `detail.additionalLocations` holds the rest, so a multi-office posting can be
    described properly instead of as "2 Locations". The listing's `locationsText`
    is still the last resort, but only when it names somewhere — the placeholder
    is worth less than nothing, since it defeats every downstream parser while
    looking like data.
    """
    primary = detail.get("location")
    extra = [p for p in (detail.get("additionalLocations") or []) if isinstance(p, str)]
    places = [p.strip() for p in ([primary] if isinstance(primary, str) else []) + extra
              if isinstance(p, str) and p.strip()]
    if places:
        return "; ".join(dict.fromkeys(places))
    fallback = posting.get("locationsText")
    if isinstance(fallback, str) and fallback.strip() and not _PLACEHOLDER_LOCATION.match(fallback):
        return fallback.strip()
    return None


def _country(detail: dict[str, Any]) -> str | None:
    """The ISO code where Workday publishes one, its free-text name otherwise.

    `country.descriptor` is what this used to read, and it is the reason the
    archive held "United States of America" alongside "US" and "USA" for the
    same country. `alpha2Code` is in the same payload, one level down.
    """
    req = detail.get("jobRequisitionLocation") or {}
    for holder in (req.get("country"), detail.get("country")):
        if isinstance(holder, dict):
            code = holder.get("alpha2Code")
            if isinstance(code, str) and code.strip():
                return code.strip().upper()
    for holder in (req.get("country"), detail.get("country")):
        if isinstance(holder, dict):
            name = holder.get("descriptor")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


class Workday(BoardSource):
    name = "workday"
    ats = "workday"
    note = "Public CXS API; needs a detail call per job for the exact date."

    async def _page(
        self,
        fetcher: Fetcher,
        base: str,
        query: str,
        offset: int,
        facets: dict[str, list[str]] | None = None,
    ) -> dict[str, Any] | None:
        data = await fetcher.post_json(
            f"{base}/jobs",
            {
                "appliedFacets": facets or {},
                "limit": PAGE_SIZE,
                "offset": offset,
                "searchText": query,
            },
            headers={"content-type": "application/json", "accept": "application/json"},
        )
        return data if isinstance(data, dict) else None

    async def _search(
        self,
        fetcher: Fetcher,
        base: str,
        query: str,
        limit: int,
        facets: dict[str, list[str]] | None = None,
    ) -> list[dict[str, Any]]:
        """Page through one query/facet combination, up to `limit` postings."""
        found: list[dict[str, Any]] = []
        offset = 0
        total = None
        while offset < limit:
            data = await self._page(fetcher, base, query, offset, facets)
            if data is None:
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

    async def _enumerate(
        self,
        fetcher: Fetcher,
        base: str,
        limit: int,
        facets: dict[str, list[str]] | None = None,
        depth: int = 0,
    ) -> list[dict[str, Any]]:
        """Enumerate a board, narrowing by facet when the total is capped.

        Workday answers `total: 2000` for anything at or above that many, so a
        board reporting exactly the cap is hiding an unknown number of postings.
        Applying a facet narrows the query until each branch reports a real
        count that can be paged to the end.

        Two things the upstream implementations don't account for, both measured:

        * Facet names are **tenant-specific** — NVIDIA exposes
          ``locationMainGroup``, not the ``locations`` a hardcoded list would
          expect — so the facet to split on is read from the response.
        * Once a facet is applied, the **nested facet counts come back as 0**.
          So a deeper split cannot be driven by counts; instead any branch that
          *itself* reports the cap is recursed into, enumerating its facet
          values blindly.
        """
        first = await self._page(fetcher, base, "", 0, facets)
        if first is None:
            return []
        total = int(first.get("total") or 0)

        if total < REPORTED_TOTAL_CAP or depth >= MAX_SUBDIVISION_DEPTH:
            # Either a real count we can page to the end of, or we've narrowed
            # as far as we're willing to. Take what this branch offers.
            return await self._search(fetcher, base, "", limit, facets)

        # Pick a facet to split on: the one offering the most values, skipping
        # any already applied (re-applying would return the same branch).
        applied = set((facets or {}).keys())
        candidates = [
            f for f in (first.get("facets") or [])
            if isinstance(f, dict)
            and f.get("facetParameter") not in applied
            and len(f.get("values") or []) > 1
        ]
        if not candidates:
            # Nothing left to narrow by; take the capped page set rather than
            # nothing. Under-collecting beats collecting zero.
            return await self._search(fetcher, base, "", limit, facets)

        facet = max(candidates, key=lambda f: len(f.get("values") or []))
        parameter = facet["facetParameter"]

        collected: list[dict[str, Any]] = []
        for value in facet.get("values") or []:
            value_id = value.get("id")
            if not value_id:
                continue
            branch = dict(facets or {})
            branch[parameter] = [value_id]
            collected.extend(
                await self._enumerate(fetcher, base, limit, branch, depth + 1)
            )
        return collected

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
        # on every tenant measured — AIG 475 postings vs 28, 3M 655 vs 41.
        # Keyword search also can't be made complete by adding terms: it misses
        # anything whose title doesn't happen to match, so it silently
        # under-collects rather than just running slower.
        #
        # `_enumerate` additionally narrows by facet when the tenant is large
        # enough to hit Workday's 2,000 reporting cap — without that, the
        # biggest employers are truncated at exactly the point they matter most.
        if queries is None:
            if with_details:
                postings = await self._enumerate(
                    fetcher, base, config.WORKDAY_MAX_PER_BOARD
                )
            else:
                # Probing only needs to prove the board answers.
                postings = await self._search(fetcher, base, "", PAGE_SIZE)
            for posting in postings:
                path = posting.get("externalPath")
                if path:
                    # Dedup by path: one posting can appear under several facet
                    # values, so branches overlap by construction.
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
            # `[]` on both paths — see the identical note in oracle.py. A
            # genuinely dead board already returned `None` above; reaching here
            # means the search answered with nothing, which is "empty".
            # Workday showed 0 empty boards out of 83 because of this line.
            return []

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
            for path, result in zip(paths, fetched, strict=True):
                if isinstance(result, dict):
                    details[path] = result

        jobs: list[Job] = []
        for path, posting in seen.items():
            detail = details.get(path, {})
            bullets = posting.get("bulletFields") or []
            req_id = bullets[0] if bullets else re.sub(r".*_", "", path)
            country = _country(detail)
            location = _location(detail, posting)
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
                    # Not `"remote" in remote_type`: Workday ships the value
                    # "Remote work is not available", which contains the word
                    # and so set this flag True — the strongest signal the
                    # classifier has, asserting the exact opposite of what the
                    # field says. `classify_hint` maps by value and checks
                    # negations first, so it is the only thing that should read
                    # this string.
                    is_remote=(classify_hint(remote_type) == "remote") or None,
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
