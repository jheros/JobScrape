"""Indeed. OPT-IN ONLY.

Scraping Indeed violates their Terms of Service. The honest, supported path is
their official partner Publisher API — apply at https://www.indeed.com/publisher.
If you obtain a key, set ``INDEED_PUBLISHER_ID`` and this adapter uses it and
never touches the unofficial route.

Without a key it can try the **mobile-app GraphQL API** that the iOS Indeed app
talks to — but that route is **currently blocked at Indeed's edge**, so it is
off by default and must be enabled with ``INDEED_ALLOW_APP_API=1``.

What was measured, on 2026-07-30, against ``apis.indeed.com/graphql``:

===========================================  ======  =========================
Request                                      Status  Who answered
===========================================  ======  =========================
POST, no ``indeed-api-key`` header              401  the API ("An API Key is
                                                     required")
POST, ``indeed-api-key`` = the real app key     403  Cloudflare
POST, ``indeed-api-key`` = ``"a"``              403  Cloudflare
POST, ``indeed-api-key`` = empty string         403  Cloudflare
POST, that same key under ``x-whatever``        401  the API
===========================================  ======  =========================

The rule keys on the **header name**, not its value: an empty ``indeed-api-key``
is refused while the identical key sent under another name sails through to a
normal API error. The API demands a key and the edge blocks the only header that
carries one, which is a deliberate closure, not a rotated credential. That also
means TLS fingerprinting cannot help — verified separately, ``curl_cffi`` with
five Chrome/Safari-iOS profiles returns the same 403 — so this is not a
candidate for the impersonating transport.

The public HTML search (``_via_html``) is likewise fronted by Cloudflare and
measured 4/4 403. **The publisher API is the only route that works.**

The GraphQL implementation is kept, tested against captured response shapes, and
left one env var away from live: it is written to the documented schema and will
work again if the rule is lifted or from a network Indeed has not fenced off.

Credit: the endpoint, its app key and the attribute filter keys come from
JobSpy (MIT) — https://github.com/speedyapply/JobSpy. Reimplemented here against
our own ``Fetcher`` and ``Job`` model, so the SSRF guard, the per-host rate
limiter, the disk cache and retry/backoff all still apply.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any

from ...http import Fetcher
from ...models import Job, annualise, html_to_text
from ..base import DEFAULT_QUERIES, GlobalSource, JobBatch

log = logging.getLogger("jobscrape.indeed")

PUBLISHER_API = "https://api.indeed.com/ads/apisearch"
GRAPHQL_API = "https://apis.indeed.com/graphql"
HTML_SEARCH = "https://www.indeed.com/jobs"

#: The iOS app's API key, shipped in the app binary and constant across clients.
#: It identifies the *app*, not a user, and carries no entitlement of ours.
APP_API_KEY = "161092c2017b5bbab13edb12461a62d5a833871e7cad6d9d475304573de67ac8"

#: Attribute keys Indeed uses for its own facets. Opaque ids, not enums.
ATTR_FULL_TIME = "CF3CP"
ATTR_REMOTE = "DSQF7"

#: Hard maximum the endpoint accepts; larger values are rejected.
PAGE_SIZE = 100
#: Pages per search term. 100/page, so this walks 2,000 postings per term
#: before moving on — past the point where relevance ordering stops paying.
MAX_PAGES = 20

_MOSAIC_RE = re.compile(
    r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*(\{.*?\});', re.S
)

#: How the iOS app identifies itself. Sent verbatim — note that
#: `indeed-api-key` is the header Indeed's edge now refuses outright.
_APP_HEADERS = {
    "Host": "apis.indeed.com",
    "content-type": "application/json",
    "indeed-api-key": APP_API_KEY,
    "accept": "application/json",
    "indeed-locale": "en-US",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6_1 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
    ),
    "indeed-app-info": "appv=193.1; appid=com.indeed.jobsearch; osv=16.6.1; os=ios; dtype=phone",
}

_QUERY = """
query GetJobData {{
  jobSearch(
    what: {what}
    location: {{ where: {where}, radius: 50, radiusUnit: MILES }}
    limit: {limit}
    {cursor}
    sort: RELEVANCE
    {filters}
  ) {{
    pageInfo {{ nextCursor }}
    results {{
      trackingKey
      job {{
        source {{ name }}
        key
        title
        datePublished
        dateOnIndeed
        description {{ html }}
        location {{
          countryName countryCode admin1Code city postalCode streetAddress
          formatted {{ short long }}
        }}
        compensation {{
          baseSalary {{ unitOfWork range {{ ... on Range {{ min max }} }} }}
          estimated {{
            currencyCode
            baseSalary {{ unitOfWork range {{ ... on Range {{ min max }} }} }}
          }}
          currencyCode
        }}
        attributes {{ key label }}
        employer {{ relativeCompanyPageUrl name }}
        recruit {{ viewJobUrl detailedSalary workSchedule }}
      }}
    }}
  }}
}}
"""



def _gql_str(value: str) -> str:
    """Embed a value in the query text. The API rejects GraphQL variables here."""
    return json.dumps(value)


class Indeed(GlobalSource):
    name = "indeed"
    note = "OPT-IN. Needs INDEED_PUBLISHER_ID; the app API is edge-blocked."

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
        if os.environ.get("INDEED_ALLOW_APP_API", "") in ("1", "true", "yes"):
            return await self._via_graphql(fetcher, queries, since, until, max_results)
        log.warning(
            "Indeed skipped: no INDEED_PUBLISHER_ID. The app API is blocked at "
            "Indeed's edge on the indeed-api-key header itself (see module "
            "docstring); set INDEED_ALLOW_APP_API=1 to attempt it anyway."
        )
        return JobBatch([])

    # --- app GraphQL API --------------------------------------------------

    def _filters(self, since: date | None) -> str:
        """Server-side narrowing, so the date window costs no wasted pages.

        Indeed accepts either a date filter or an attribute filter, not both in
        the same shorthand, so the date window wins when one is asked for — it
        removes far more rows than a full-time facet would.
        """
        if since is not None:
            hours = max(int((date.today() - since).days) * 24, 24)
            return (
                'filters: { date: { field: "dateOnIndeed", start: "%dh" } }' % hours
            )
        return (
            "filters: { composite: { filters: [{ keyword: "
            '{ field: "attributes", keys: ["%s"] } }] } }' % ATTR_FULL_TIME
        )

    async def _via_graphql(
        self,
        fetcher: Fetcher,
        queries: list[str] | None,
        since: date | None,
        until: date | None,
        max_results: int | None,
    ) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        filters = self._filters(since)

        for term in queries or DEFAULT_QUERIES:
            cursor: str | None = None
            for page in range(MAX_PAGES):
                body = {
                    "query": _QUERY.format(
                        what=_gql_str(term),
                        where=_gql_str("United States"),
                        limit=PAGE_SIZE,
                        cursor=f"cursor: {_gql_str(cursor)}" if cursor else "",
                        filters=filters,
                    )
                }
                data = await fetcher.post_json(GRAPHQL_API, body, headers=_APP_HEADERS)
                if not isinstance(data, dict):
                    break
                if data.get("errors"):
                    log.warning("Indeed GraphQL error on %r: %s", term, data["errors"])
                    break
                search = (data.get("data") or {}).get("jobSearch") or {}
                results = search.get("results") or []
                if not results:
                    break
                for entry in results:
                    job = self._parse(entry, until)
                    if job is None or job.source_job_id in seen:
                        continue
                    seen.add(job.source_job_id)
                    jobs.append(job)
                if max_results and len(jobs) >= max_results:
                    return JobBatch(jobs)
                cursor = (search.get("pageInfo") or {}).get("nextCursor")
                if not cursor:
                    break

        if not jobs:
            log.warning(
                "Indeed returned no jobs — expected, the app API is edge-blocked. "
                "Set INDEED_PUBLISHER_ID to use their official API, which works."
            )
        # `complete` gates source-wide closure, and this is never complete:
        # relevance-ranked search over a fixed term list samples the catalogue
        # rather than enumerating it, so it must never retire postings it simply
        # did not reach. JobBatch defaults to complete=False, which is that.
        return JobBatch(jobs)

    def _parse(self, entry: dict[str, Any], until: date | None) -> Job | None:
        job = (entry or {}).get("job") or {}
        key = str(job.get("key") or "")
        if not key:
            return None

        posted = _epoch_ms(job.get("datePublished")) or _epoch_ms(job.get("dateOnIndeed"))
        if until is not None and posted is not None and posted.date() > until:
            return None

        loc = job.get("location") or {}
        formatted = loc.get("formatted") or {}
        attributes = [a.get("label") for a in (job.get("attributes") or []) if a.get("label")]
        keys = {a.get("key") for a in (job.get("attributes") or [])}

        low, high, currency = _salary(job.get("compensation") or {})
        html = ((job.get("description") or {}).get("html")) or None
        employer = job.get("employer") or {}
        recruit = job.get("recruit") or {}

        return Job(
            source=self.name,
            source_job_id=key,
            title=job.get("title") or "",
            # Indeed lists postings with no employer record (staffing feeds).
            # Falling back to the aggregator name would create one giant fake
            # "company", so those are left blank for the filters to judge.
            company=employer.get("name") or "",
            job_url=f"https://www.indeed.com/viewjob?jk={key}",
            apply_url=recruit.get("viewJobUrl") or f"https://www.indeed.com/viewjob?jk={key}",
            location_raw=formatted.get("long") or formatted.get("short"),
            city=loc.get("city"),
            state=loc.get("admin1Code"),
            country=loc.get("countryCode") or loc.get("countryName"),
            is_remote=True if ATTR_REMOTE in keys else None,
            employment_type=recruit.get("workSchedule") or (attributes[0] if attributes else None),
            posted_at=posted.isoformat() if posted else None,
            salary_min=low,
            salary_max=high,
            salary_currency=currency,
            salary_raw=recruit.get("detailedSalary"),
            description_html=html,
            description_text=html_to_text(html) or None,
            raw=job,
        )

    # --- official publisher API -------------------------------------------

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
                    return JobBatch(jobs)
        return JobBatch(jobs)

    # --- legacy HTML scrape ------------------------------------------------
    # Kept only as a last resort. Measured 4/4 HTTP 403 — Cloudflare fronts the
    # public search — so it is no longer on the default path.

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
                "Indeed blocked %d HTML request(s). This is the expected outcome; "
                "the app API is the working path.",
                blocked,
            )
        return JobBatch(jobs)

    def _parse_mosaic(self, html: str) -> list[Job]:
        """Extract the embedded job-card JSON blob, when it survives."""
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


def _epoch_ms(value: Any) -> datetime | None:
    """Indeed dates are epoch MILLISECONDS; read as seconds they land in 1970."""
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)


def _salary(compensation: dict[str, Any]) -> tuple[float | None, float | None, str | None]:
    """Read the employer-stated range, annualised.

    ``estimated`` is deliberately ignored: it is Indeed's own model output, not
    something the employer said, and storing it beside real ranges would make
    the salary column mean two different things.
    """
    base = compensation.get("baseSalary") or {}
    rng = base.get("range") or {}
    low, high = rng.get("min"), rng.get("max")
    if low is None and high is None:
        return None, None, None
    # An unrecognised unit yields no salary rather than an unconverted number:
    # a $30 "salary" is worse than a blank, because it silently joins every
    # salary filter and chart as if it were annual.
    unit = base.get("unitOfWork")
    return annualise(low, unit), annualise(high, unit), compensation.get("currencyCode")
