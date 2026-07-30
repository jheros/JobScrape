"""Direct employer career sites.

Amazon, Apple and Uber each run their own careers API rather than an ATS we
already cover, so their openings are invisible to every other adapter here.
They are :class:`GlobalSource` rather than :class:`BoardSource` because there is
no slug to discover — each is a single, fixed employer whose whole catalogue is
enumerable, which is also what makes source-wide closure correct for them.

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
_MAX_MERGED_LOCATIONS = 8


def _format_locations(places: list[str]) -> str | None:
    """Render an accumulated location list, noting anything elided.

    Built from the full list every time rather than by appending to the
    previous string — re-parsing a rendered ``"…; +3 more"`` would count the
    suffix as a place and inflate the total on the next merge.
    """
    unique = list(dict.fromkeys(p for p in places if p))
    if not unique:
        return None
    if len(unique) > _MAX_MERGED_LOCATIONS:
        extra = len(unique) - _MAX_MERGED_LOCATIONS
        return "; ".join(unique[:_MAX_MERGED_LOCATIONS]) + f"; +{extra} more"
    return "; ".join(unique)


# =========================================================================
# Amazon
# =========================================================================

AMAZON_SEARCH = "https://www.amazon.jobs/en/search.json"
#: Amazon's own maximum; larger values are clamped server-side.
AMAZON_PAGE = 100
#: Amazon stops serving results past 10,000 for any one query, so a query that
#: reports more than this is truncated and has to be split by facet.
AMAZON_RESULT_CAP = 10_000


class Amazon(GlobalSource):
    """amazon.jobs, via the keyless JSON behind its search page.

    One of the largest single software employers, and entirely absent from every
    ATS adapter here — Amazon runs its own stack.

    The response carries ``basic_qualifications`` and ``preferred_qualifications``
    separately from ``description``. All three are joined: a posting's real
    requirements live in the qualification fields, so keeping only
    ``description`` would drop the part that matters most for role filtering.
    """

    name = "amazon"
    note = "Keyless JSON search on amazon.jobs; ~1,200 US software postings."

    async def _page(
        self, fetcher: Fetcher, query: str, offset: int, category: str | None
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {
            "base_query": query,
            "loc_query": "United States",
            "country": "USA",
            "result_limit": AMAZON_PAGE,
            "offset": offset,
            "sort": "recent",
        }
        if category:
            params["business_category[]"] = category
        data = await fetcher.get_json(
            AMAZON_SEARCH, params=params, headers={"User-Agent": _BROWSER_UA}
        )
        return data if isinstance(data, dict) else None

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
        # A single empty query enumerates the US board; the caller's `queries`
        # only narrow it. Enumerating beats keyword search for the same reason
        # it does on Workday — a term list silently misses whatever it does not
        # happen to match.
        terms = queries or ["software engineer"]
        for term in terms:
            offset = 0
            while True:
                data = await self._page(fetcher, term, offset, None)
                if data is None:
                    complete = False
                    break
                rows = data.get("jobs") or []
                if not rows:
                    break
                for row in rows:
                    job = self._parse(row)
                    if job is not None:
                        seen.setdefault(job.source_job_id, job)
                hits = int(data.get("hits") or 0)
                offset += AMAZON_PAGE
                if offset >= min(hits, AMAZON_RESULT_CAP):
                    if hits > AMAZON_RESULT_CAP:
                        # Past this Amazon simply stops serving rows, so the
                        # remainder is unreachable without splitting the query.
                        log.info(
                            "Amazon reported %d hits for %r but serves at most "
                            "%d; the rest were not reached",
                            hits, term, AMAZON_RESULT_CAP,
                        )
                        complete = False
                    break
                if max_results and len(seen) >= max_results:
                    complete = False
                    break

        jobs = list(seen.values())
        return JobBatch(jobs, complete=complete and bool(jobs))

    def _parse(self, row: dict[str, Any]) -> Job | None:
        job_id = str(row.get("id_icims") or row.get("id") or "")
        path = row.get("job_path") or ""
        if not job_id or not path:
            return None
        # The requirements are in their own fields, not in `description`.
        body = "\n\n".join(
            part for part in (
                row.get("description"),
                row.get("basic_qualifications"),
                row.get("preferred_qualifications"),
            ) if isinstance(part, str) and part.strip()
        )
        city, state = row.get("city"), row.get("state")
        return Job(
            source=self.name,
            source_job_id=job_id,
            title=_unescape(row.get("title")) or "",
            company="Amazon",
            company_slug="amazon",
            job_url=f"https://www.amazon.jobs{path}",
            apply_url=f"https://www.amazon.jobs{path}",
            location_raw=row.get("normalized_location") or row.get("location"),
            city=city,
            state=state,
            country=row.get("country_code"),
            employment_type=row.get("job_schedule_type"),
            department=row.get("business_category") or row.get("job_category"),
            # "July 30, 2026" — a display string, not a timestamp.
            posted_at=_us_long_date(row.get("posted_date")),
            updated_at=row.get("updated_time"),
            description_html=body or None,
            description_text=html_to_text(body) or None,
            raw=row,
        )


def _us_long_date(value: Any) -> str | None:
    """Parse ``"July 30, 2026"``, the only date shape these pages emit."""
    if not isinstance(value, str) or not value.strip():
        return None
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(
                tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return value


# =========================================================================
# Apple
# =========================================================================

APPLE_BASE = "https://jobs.apple.com"
APPLE_CSRF = f"{APPLE_BASE}/api/v1/csrfToken"
APPLE_SEARCH = f"{APPLE_BASE}/api/v1/search"
#: Apple's fixed page size; the payload has no size field.
APPLE_PAGE = 20
#: 4,611 US postings at 20/page. The cap is a runaway guard, not a limit
#: expected to bind.
APPLE_MAX_PAGES = 400


class Apple(GlobalSource):
    """jobs.apple.com, behind a CSRF bootstrap.

    Two things measured here that a plausible-looking payload gets wrong:

    * The location filter key is ``locations``. ``postingpostLocation`` and
      ``postLocation`` are **silently ignored** — they return HTTP 200 and the
      full 6,041-posting worldwide set, so a wrong key looks like a working one
      until you check the countries in the response. With ``locations`` the
      total drops to 4,611, all United States.
    * Results are nested under ``res``, not at the top level, so a reader that
      looks for ``searchResults`` directly finds nothing and reports an empty
      board rather than an error.
    """

    name = "apple"
    note = "CSRF-bootstrapped search on jobs.apple.com; ~4,600 US postings."

    async def _token(self, fetcher: Fetcher) -> str | None:
        """The token arrives as a response *header*, with an empty body."""
        headers = await fetcher.get_headers(
            APPLE_CSRF, headers={"User-Agent": _BROWSER_UA}
        )
        if not headers:
            return None
        return headers.get("X-Apple-CSRF-Token") or headers.get("x-apple-csrf-token")

    async def fetch(
        self,
        fetcher: Fetcher,
        since: date | None = None,
        until: date | None = None,
        queries: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[Job]:
        token = await self._token(fetcher)
        if not token:
            log.warning("Apple: no CSRF token; search cannot be called")
            return JobBatch([])

        seen: dict[str, Job] = {}
        #: requisition id -> every site it was posted at, in first-seen order.
        places: dict[str, list[str]] = {}
        complete = True
        for page in range(1, APPLE_MAX_PAGES + 1):
            data = await fetcher.post_json(
                APPLE_SEARCH,
                {
                    "query": (queries or [""])[0],
                    "filters": {"locations": ["postLocation-USA"]},
                    "page": page,
                    "locale": "en-us",
                    "sort": "newest",
                    "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"},
                },
                headers={
                    "X-Apple-CSRF-Token": token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": _BROWSER_UA,
                },
            )
            result = (data or {}).get("res") if isinstance(data, dict) else None
            if not isinstance(result, dict):
                complete = False
                break
            rows = result.get("searchResults") or []
            if not rows:
                break
            for row in rows:
                job = self._parse(row)
                if job is None:
                    continue
                # Apple's `id` is `{positionId}-{storeCode}`: one requisition
                # reposted per store, so 160 rows carried only 128 requisitions
                # in a sample. Key on the requisition and merge the store
                # locations into it — the same choice the Workable adapter
                # makes for the same reason. Taking the first and discarding
                # the rest would silently throw the other locations away.
                seen.setdefault(job.source_job_id, job)
                places.setdefault(job.source_job_id, []).extend(
                    p.strip() for p in (job.location_raw or "").split(";") if p.strip()
                )
            total = int(result.get("totalRecords") or 0)
            if page * APPLE_PAGE >= total or len(rows) < APPLE_PAGE:
                break
            if max_results and len(seen) >= max_results:
                complete = False
                break
        else:
            complete = False

        for job_id, job in seen.items():
            job.location_raw = _format_locations(places.get(job_id, []))
        jobs = list(seen.values())
        return JobBatch(jobs, complete=complete and bool(jobs))

    def _parse(self, row: dict[str, Any]) -> Job | None:
        position = str(row.get("positionId") or row.get("id") or "")
        if not position:
            return None
        places = [p for p in (row.get("locations") or []) if isinstance(p, dict)]
        names = [p.get("name") for p in places if p.get("name")]
        countries = {p.get("countryName") for p in places if p.get("countryName")}
        title = row.get("postingTitle") or row.get("jobSummary") or ""
        return Job(
            source=self.name,
            source_job_id=position,
            title=title,
            company="Apple",
            company_slug="apple",
            job_url=f"{APPLE_BASE}/en-us/details/{position}",
            apply_url=f"{APPLE_BASE}/en-us/details/{position}",
            location_raw="; ".join(dict.fromkeys(names)) or None,
            country=next(iter(countries), None) if len(countries) == 1 else None,
            department=row.get("team", {}).get("teamName")
            if isinstance(row.get("team"), dict) else None,
            # `postDateInGMT` is a real timestamp; `postingDate` is display text.
            posted_at=row.get("postDateInGMT") or _us_long_date(row.get("postingDate")),
            description_text=row.get("jobSummary") or None,
            raw=row,
        )


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
