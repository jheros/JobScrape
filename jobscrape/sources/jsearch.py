"""JSearch (RapidAPI) — aggregated LinkedIn / Indeed / Glassdoor / ZipRecruiter.

This is the **compliant** route to the big-three job boards. Rather than scraping
sites whose terms forbid it (see ``sources/restricted/``), JSearch licenses and
republishes those listings through its own API.

Requires a free RapidAPI key: <https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch>

The free tier is roughly 200 requests/month, which is *tiny* next to the other
sources here. Two consequences shape this adapter:

* Query count is capped hard by ``JSEARCH_MAX_REQUESTS`` and defaults low.
* Responses are cached for a full week, far longer than the global default.

**Not verified against a live response** — I had no key when writing it. Field
names follow JSearch's documented schema and every access is defensive, so an
unexpected shape yields fewer fields rather than an exception. Confirm against a
real call once a key is in place.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from .. import config
from ..http import Fetcher
from ..models import Job
from .base import DEFAULT_QUERIES, GlobalSource, JobBatch

log = logging.getLogger("jobscrape.jsearch")

# Host and path are configurable because RapidAPI job APIs come and go. As of
# writing, jsearch.p.rapidapi.com authenticates and accepts subscriptions but has
# **no registered endpoints** — every path, real or invented, returns 404
# "Endpoint does not exist", while other APIs on the same key return 403
# "not subscribed". That asymmetry proves the subscription is fine and the
# provider's routing is broken. Point RAPIDAPI_HOST/RAPIDAPI_PATH at an
# equivalent API to switch without touching code.
HOST = config.RAPIDAPI_HOST
API = f"https://{HOST}{config.RAPIDAPI_PATH}"
CACHE_TTL = 7 * 24 * 3600  # a week: the quota is far scarcer than freshness


def _first(item: dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-empty value among ``keys``.

    JSearch has renamed fields between versions (``job_apply_link`` vs
    ``job_google_link``, ``employer_name`` vs ``employer``), so each read tries
    the documented name and its known aliases.
    """
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


class JSearch(GlobalSource):
    name = "jsearch"
    requires_key = True
    note = "Aggregates LinkedIn/Indeed/Glassdoor legally. Needs RAPIDAPI_KEY (free tier is small)."

    def available(self) -> tuple[bool, str]:
        if not config.RAPIDAPI_KEY:
            return False, (
                "set RAPIDAPI_KEY (free: rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch)"
            )
        return True, ""

    def _headers(self) -> dict[str, str]:
        return {
            "X-RapidAPI-Key": config.RAPIDAPI_KEY,
            "X-RapidAPI-Host": HOST,
        }

    async def fetch(
        self,
        fetcher: Fetcher,
        since: date | None = None,
        until: date | None = None,
        queries: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[Job]:
        ok, reason = self.available()
        if not ok:
            return JobBatch()

        budget = config.JSEARCH_MAX_REQUESTS
        if budget <= 0:
            log.info("jsearch: request budget is 0, skipping")
            return JobBatch()

        # Coarse recency filter; the exact window is re-applied locally anyway.
        date_posted = "all"
        if since:
            age = (date.today() - since).days
            date_posted = "today" if age <= 1 else "week" if age <= 7 else "month" if age <= 31 else "all"

        jobs: list[Job] = []
        seen: set[str] = set()
        spent = 0

        for term in (queries or DEFAULT_QUERIES):
            if spent >= budget:
                log.info("jsearch: hit the %d-request budget, stopping", budget)
                break
            data = await fetcher.get_json(
                API,
                params={
                    "query": f"{term} in United States",
                    "page": "1",
                    "num_pages": "1",
                    "country": "us",
                    "date_posted": date_posted,
                },
                headers=self._headers(),
                cache_ttl=CACHE_TTL,
                retries=1,
            )
            spent += 1
            if not isinstance(data, dict):
                # Distinguish a provider outage from an empty result set: a
                # first query returning nothing usually means the host/path is
                # wrong or unrouted, not that there are no software jobs.
                if spent == 1:
                    log.warning(
                        "jsearch: no response from %s. If this is a 404, the API has no "
                        "registered endpoints (a provider-side fault, not your key) — "
                        "set RAPIDAPI_HOST/RAPIDAPI_PATH to a working alternative.",
                        API,
                    )
                continue
            for item in data.get("data") or []:
                job = self._parse(item)
                if job and job.source_job_id not in seen:
                    seen.add(job.source_job_id)
                    jobs.append(job)
                    if max_results and len(jobs) >= max_results:
                        return JobBatch(jobs, complete=False)
        # Never complete, by construction: the free tier is ~200 requests/month,
        # so JSEARCH_MAX_REQUESTS deliberately samples rather than enumerates.
        # A sampled run must never close the postings it did not sample.
        return JobBatch(jobs, complete=False)

    def _parse(self, item: Any) -> Job | None:
        if not isinstance(item, dict):
            return None
        job_id = _first(item, "job_id", "job_posting_id")
        if not job_id:
            return None

        city = _first(item, "job_city")
        state = _first(item, "job_state")
        country = _first(item, "job_country")
        location = _first(item, "job_location") or ", ".join(
            str(p) for p in (city, state, country) if p
        )

        is_remote = _first(item, "job_is_remote")
        publisher = _first(item, "job_publisher", "employer_name")

        def _num(value: Any) -> float | None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        return Job(
            source=self.name,
            source_job_id=str(job_id),
            title=_first(item, "job_title") or "",
            company=_first(item, "employer_name", "employer") or "",
            job_url=_first(item, "job_apply_link", "job_google_link") or "",
            apply_url=_first(item, "job_apply_link"),
            location_raw=location or None,
            city=city,
            state=state,
            country=country,
            is_remote=bool(is_remote) if is_remote is not None else None,
            workplace_hint="remote" if is_remote else None,
            employment_type=_first(item, "job_employment_type"),
            # Which board it came from (LinkedIn, Indeed, …) — useful provenance.
            department=str(publisher) if publisher else None,
            posted_at=_first(
                item, "job_posted_at_datetime_utc", "job_posted_at_timestamp"
            ),
            salary_min=_num(_first(item, "job_min_salary")),
            salary_max=_num(_first(item, "job_max_salary")),
            salary_currency=_first(item, "job_salary_currency"),
            description_html=_first(item, "job_description"),
            raw=item,
        )
