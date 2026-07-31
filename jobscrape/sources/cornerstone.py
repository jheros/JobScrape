"""Cornerstone OnDemand (CSOD) career sites.

Cornerstone hosts a long tail of US enterprises, universities and health
systems. The flow is bootstrap-then-search:

1. ``GET https://{tenant}.csod.com/ux/ats/careersite/{site}/home?c={tenant}``
   carries a short-lived JWT in ``csod.context.token`` **and** the tenant's
   regional API host.
2. ``POST {api_host}/rec-job-search/external/jobs`` with that JWT as a bearer
   token returns ``totalCount`` plus a page of ``requisitions``.

A previous pass here concluded Cornerstone was "auth-gated, not viable". That
was wrong, and wrong for an avoidable reason: the endpoint was guessed at
instead of read off the page. Two things a guess gets wrong —

* the **API host is regional and per-tenant**. Sampling 24 tenants turned up
  ``us.``, ``uk.``, ``eu-fra.``, ``eu-cdg.`` and ``au.`` — so it has to be read
  from the page, never assumed. ``na.`` (the value most references cite) did
  not appear once.
* the search rejects the token without ``Origin``/``Referer`` naming the
  tenant's own career site.

With both right, 22 of those 24 tenants answered 200.

A company is addressed as ``tenant|site_id``, e.g. ``biola|1``. The site id is
part of the identity, not a detail: one tenant commonly runs several career
sites (internal, campus, per-brand) and each lists different jobs.

Credit: bootstrap shape from ats-scrapers (MIT) —
https://github.com/kalil0321/ats-scrapers
"""

from __future__ import annotations

import re
from typing import Any

from ..http import Fetcher
from ..models import Job, html_to_text
from .base import BoardSource, Prefilter, shallow_refresh

CAREER_SITE = "https://{tenant}.csod.com/ux/ats/careersite/{site}/home?c={tenant}"
JOB_PAGE = "https://{tenant}.csod.com/ux/ats/careersite/{site}/job/{req}?c={tenant}"
SEARCH_PATH = "/rec-job-search/external/jobs"

#: Cornerstone's own page size. Larger values are accepted but the response is
#: still capped at 25, so asking for more just misreports how far a page got.
PAGE_SIZE = 25
#: Bounds a single board. 25/page, so 40 pages — well past any real tenant.
MAX_PAGES = 40

_TOKEN_RE = re.compile(r"""csod\.context\.token\s*=\s*['"]([^'"]+)['"]""")
#: Some tenants render the bootstrap as JSON instead of an assignment.
_TOKEN_JSON_RE = re.compile(r'"token"\s*:\s*"([^"]{20,})"')
_API_HOST_RE = re.compile(r"https?://([a-z0-9-]+\.api\.csod\.com)", re.I)

#: The API host comes out of a page we do not control, so it is a request
#: target chosen by remote content — exactly the shape an SSRF guard exists
#: for. The guard blocks private addresses; this additionally pins the host to
#: Cornerstone's own domain, so a compromised or hostile career page cannot
#: point us at some unrelated public host and have us forward a bearer token.
_API_DOMAIN = ".api.csod.com"

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: Tenants who never filled in a public description leave a placeholder behind.
#: Storing these is worse than storing nothing: they read as real text, so they
#: defeat the "has a description" checks downstream instead of failing them.
_PLACEHOLDERS = frozenset({
    "please upload the job description",
    "please upload a job description",
    "please add the job description",
    "no description available",
    "see job description",
    "to be confirmed",
    "n/a", "na", "tba", "tbc", "-", ".",
})


def parse_slug(slug: str) -> tuple[str, int] | None:
    """Split ``tenant|site_id``, defaulting the site to 1."""
    parts = [p.strip() for p in slug.split("|")]
    tenant = parts[0]
    # The tenant becomes a hostname label, so anything that could escape it
    # (a dot, a slash, an @) is rejected rather than sanitised.
    if not tenant or not re.fullmatch(r"[A-Za-z0-9-]+", tenant):
        return None
    if len(parts) > 1 and parts[1]:
        try:
            site = int(parts[1])
        except ValueError:
            return None
        if site < 0:
            return None
        return tenant, site
    return tenant, 1


def _api_host(page: str) -> str | None:
    """Read the tenant's regional API host out of the bootstrap page."""
    match = _API_HOST_RE.search(page)
    if not match:
        return None
    host = match.group(1).lower()
    # Belt and braces: the regex already anchors on the domain, but the
    # allowlist is what the security property should rest on, not the regex.
    return host if host.endswith(_API_DOMAIN) else None


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = html_to_text(value).strip()
    if not text or text.lower() in _PLACEHOLDERS:
        return None
    return text


def _location(value: Any) -> str | None:
    """Flatten ``locations``: a list of dicts, or of plain strings."""
    if not isinstance(value, list) or not value:
        return None
    labels: list[str] = []
    for place in value[:4]:
        if isinstance(place, str) and place.strip():
            labels.append(place.strip())
        elif isinstance(place, dict):
            parts = [
                str(place[k]).strip()
                for k in ("city", "state", "country")
                if isinstance(place.get(k), str) and place[k].strip()
            ]
            labels.append(", ".join(parts) if parts
                          else str(place.get("name") or place.get("displayName") or "").strip())
    return "; ".join(dict.fromkeys(l for l in labels if l)) or None


def _country(value: Any) -> str | None:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        country = value[0].get("country")
        if isinstance(country, str) and country.strip():
            return country.strip()
    return None


class Cornerstone(BoardSource):
    name = "cornerstone"
    ats = "cornerstone"
    note = "JWT + regional API host bootstrapped from the career page."

    async def _bootstrap(
        self, fetcher: Fetcher, tenant: str, site: int
    ) -> tuple[str, str, str] | None:
        """Return ``(token, api_host, origin)``, or None if this is not a board."""
        origin = f"https://{tenant}.csod.com"
        page = await fetcher.get_text(
            CAREER_SITE.format(tenant=tenant, site=site),
            headers={"User-Agent": _BROWSER_UA,
                     "Accept": "text/html,application/xhtml+xml"},
        )
        if not page:
            return None
        match = _TOKEN_RE.search(page) or _TOKEN_JSON_RE.search(page)
        host = _api_host(page)
        if not match or not host:
            return None
        return match.group(1), host, origin

    async def _search(
        self,
        fetcher: Fetcher,
        token: str,
        api_host: str,
        origin: str,
        site: int,
        page: int,
    ) -> dict[str, Any] | None:
        data = await fetcher.post_json(
            f"https://{api_host}{SEARCH_PATH}",
            {
                "careerSiteId": site,
                "careerSitePageId": site,
                "pageNumber": page,
                "pageSize": PAGE_SIZE,
                "cultureId": 1,
                "cultureName": "en-US",
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                # Without these the token is refused: the API checks that the
                # caller claims to be the tenant's own career site.
                "Origin": origin,
                "Referer": f"{origin}/",
                "User-Agent": _BROWSER_UA,
            },
        )
        if not isinstance(data, dict):
            return None
        inner = data.get("data")
        return inner if isinstance(inner, dict) else None

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        known_ids: set[str] | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        parsed = parse_slug(slug)
        if not parsed:
            return None
        tenant, site = parsed

        session = await self._bootstrap(fetcher, tenant, site)
        if session is None:
            return None
        token, api_host, origin = session

        first = await self._search(fetcher, token, api_host, origin, site, 1)
        if first is None:
            # The page bootstrapped but the search refused — observed as a
            # transient on two boards that returned 263 and 61 postings a
            # minute later. `None`, not `[]`: an empty list marks the board
            # *successful*, which resets its health counter and would let a
            # board whose API always refuses look healthy forever while
            # yielding nothing. `None` backs off and still closes no postings.
            return None
        total = int(first.get("totalCount") or 0)
        rows = list(first.get("requisitions") or [])

        if with_details:
            page = 2
            while len(rows) < total and page <= MAX_PAGES:
                data = await self._search(fetcher, token, api_host, origin, site, page)
                batch = (data or {}).get("requisitions") or []
                if not batch:
                    break
                rows.extend(batch)
                page += 1

        seen: dict[str, dict[str, Any]] = {}
        for item in rows:
            if isinstance(item, dict) and item.get("requisitionId"):
                seen.setdefault(str(item["requisitionId"]), item)
        if not seen:
            return []

        known = known_ids or set()
        jobs: list[Job] = []
        for req_id, item in seen.items():
            title = (item.get("displayJobTitle") or item.get("jobTitle") or "").strip()
            locations = item.get("locations")
            if prefilter is not None and not prefilter(
                title, item.get("department"), _location(locations), _country(locations)
            ):
                continue
            body = item.get("externalDescription") or item.get("description")
            description = _clean(body)
            job = Job(
                source=self.name,
                source_job_id=f"{tenant}:{req_id}",
                title=title,
                company=tenant,
                company_slug=slug,
                job_url=JOB_PAGE.format(tenant=tenant, site=site, req=req_id),
                apply_url=JOB_PAGE.format(tenant=tenant, site=site, req=req_id),
                location_raw=_location(locations),
                country=_country(locations),
                employment_type=item.get("schedule") or item.get("jobType"),
                department=item.get("department") or item.get("category"),
                # Cornerstone ships this as US-locale M/D/YYYY on most tenants
                # and ISO on the rest; `to_utc_iso` handles both.
                posted_at=item.get("postingEffectiveDate"),
                description_html=body if description else None,
                description_text=description,
                raw=item,
            )
            if f"{tenant}:{req_id}" in known:
                job = shallow_refresh(job)
            jobs.append(job)
        return jobs

    async def probe(self, fetcher: Fetcher, slug: str) -> tuple[str, int]:
        """One bootstrap + one search page — never the full pagination."""
        jobs = await self.fetch_board(fetcher, slug, with_details=False)
        if jobs is None:
            return "dead", 0
        return ("live", len(jobs)) if jobs else ("empty", 0)
