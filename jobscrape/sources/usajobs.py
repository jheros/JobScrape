"""USAJobs — the official US federal job API.

Requires a free API key (register at https://developer.usajobs.gov/apirequest/).
Without one the adapter reports itself unavailable and is skipped; it never
fails a run.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .. import config
from ..http import Fetcher
from ..models import Job
from .base import DEFAULT_QUERIES, GlobalSource, JobBatch

API = "https://data.usajobs.gov/api/search"
PAGE_SIZE = 500  # the API's documented maximum


def _remote_indicator(value: Any) -> bool | None:
    """USAJobs sends ``RemoteIndicator`` as the *string* "true"/"false"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


class USAJobs(GlobalSource):
    name = "usajobs"
    requires_key = True
    note = "Federal jobs. Needs a free USAJOBS_API_KEY."

    def available(self) -> tuple[bool, str]:
        if not config.USAJOBS_API_KEY:
            return False, "set USAJOBS_API_KEY (free: developer.usajobs.gov/apirequest)"
        return True, ""

    def _headers(self) -> dict[str, str]:
        return {
            "Host": "data.usajobs.gov",
            "User-Agent": config.USAJOBS_EMAIL,
            "Authorization-Key": config.USAJOBS_API_KEY,
        }

    async def fetch(
        self,
        fetcher: Fetcher,
        since: date | None = None,
        until: date | None = None,
        queries: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[Job]:
        ok, _ = self.available()
        if not ok:
            return JobBatch()

        jobs: list[Job] = []
        seen: set[str] = set()
        # See JobBatch — only an exhaustive crawl may be used to close postings.
        complete = True
        for term in queries or DEFAULT_QUERIES:
            page = 1
            exhausted = False
            while True:
                params: dict[str, Any] = {
                    "Keyword": term,
                    "ResultsPerPage": PAGE_SIZE,
                    "Page": page,
                    "SortField": "opendate",
                    "SortDirection": "desc",
                }
                if since:
                    params["DatePosted"] = min((date.today() - since).days, 60)
                data = await fetcher.get_json(API, params=params, headers=self._headers())
                if not isinstance(data, dict):
                    complete = False  # the page failed; the rest is unread
                    break
                result = data.get("SearchResult") or {}
                items = result.get("SearchResultItems") or []
                if not items:
                    exhausted = True
                    break
                for entry in items:
                    job = self._parse(entry)
                    if job and job.source_job_id not in seen:
                        seen.add(job.source_job_id)
                        jobs.append(job)
                if len(items) < PAGE_SIZE:
                    exhausted = True
                    break
                if page >= 10:
                    # Hit the per-term page ceiling with a full page in hand,
                    # so this term had more than we read.
                    complete = False
                    break
                page += 1
            if not exhausted:
                complete = False
            if max_results and len(jobs) >= max_results:
                complete = False  # capped, not exhausted
                break
        return JobBatch(jobs, complete=complete)

    def _parse(self, entry: dict[str, Any]) -> Job | None:
        descriptor = (entry or {}).get("MatchedObjectDescriptor") or {}
        job_id = entry.get("MatchedObjectId") or descriptor.get("PositionID")
        if not job_id:
            return None

        remuneration = (descriptor.get("PositionRemuneration") or [{}])[0]
        locations = descriptor.get("PositionLocation") or []
        location_raw = descriptor.get("PositionLocationDisplay") or ", ".join(
            str(loc.get("LocationName")) for loc in locations[:3] if loc.get("LocationName")
        )
        detail = descriptor.get("UserArea", {}).get("Details", {}) or {}
        summary = detail.get("JobSummary") or ""
        duties = descriptor.get("QualificationSummary") or ""
        html = "\n\n".join(p for p in (summary, duties) if p)

        def _num(value: Any) -> float | None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        return Job(
            source=self.name,
            source_job_id=str(job_id),
            title=descriptor.get("PositionTitle") or "",
            company=descriptor.get("OrganizationName") or "US Federal Government",
            job_url=descriptor.get("PositionURI") or "",
            apply_url=(descriptor.get("ApplyURI") or [None])[0],
            location_raw=location_raw or None,
            country="US",
            # A string "true"/"false", not a boolean. Present on essentially
            # every federal posting and previously unread, which is why 116 of
            # this source's 117 rows sat at "unknown". Used only as a fallback
            # inside `classify_workplace`, so wording in the posting still wins.
            is_remote=_remote_indicator(detail.get("RemoteIndicator")),
            employment_type=", ".join(
                t.get("Name", "") for t in descriptor.get("PositionSchedule") or []
            )
            or None,
            department=descriptor.get("DepartmentName"),
            posted_at=descriptor.get("PublicationStartDate"),
            salary_min=_num(remuneration.get("MinimumRange")),
            salary_max=_num(remuneration.get("MaximumRange")),
            salary_currency=remuneration.get("RateIntervalCode") and "USD",
            description_html=html or None,
            raw=descriptor,
        )
