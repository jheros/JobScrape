"""Arbeitnow public job board feed.

Skews heavily European, so US yield is modest — but it is free, keyless, and
the location filter discards the rest cheaply.
"""

from __future__ import annotations

from datetime import date

from ..http import Fetcher
from ..models import Job
from .base import GlobalSource, JobBatch

API = "https://www.arbeitnow.com/api/job-board-api"
MAX_PAGES = 20


class Arbeitnow(GlobalSource):
    name = "arbeitnow"
    note = "Free keyless feed; mostly EU roles, low US volume."

    async def fetch(
        self,
        fetcher: Fetcher,
        since: date | None = None,
        until: date | None = None,
        queries: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[Job]:
        jobs: list[Job] = []
        # See JobBatch — only an exhaustive crawl may be used to close postings.
        complete = True
        exhausted = False
        for page in range(1, MAX_PAGES + 1):
            data = await fetcher.get_json(API, params={"page": page})
            if not isinstance(data, dict):
                complete = False  # the page failed; the rest is unread
                break
            batch = data.get("data") or []
            if not batch:
                exhausted = True
                break
            for item in batch:
                if not isinstance(item, dict) or not item.get("slug"):
                    continue
                tags = item.get("tags") or []
                jobs.append(
                    Job(
                        source=self.name,
                        source_job_id=str(item["slug"]),
                        title=item.get("title") or "",
                        company=item.get("company_name") or "",
                        job_url=item.get("url") or "",
                        apply_url=item.get("url"),
                        location_raw=item.get("location"),
                        is_remote=bool(item.get("remote")),
                        employment_type=", ".join(item.get("job_types") or []) or None,
                        department=", ".join(str(t) for t in tags[:5]) or None,
                        posted_at=item.get("created_at"),
                        description_html=item.get("description") or None,
                        raw=item,
                    )
                )
            if max_results and len(jobs) >= max_results:
                return JobBatch(jobs, complete=False)  # capped, not exhausted
            if not (data.get("links") or {}).get("next"):
                exhausted = True  # the feed said this was the last page
                break
        return JobBatch(jobs, complete=complete and exhausted)
