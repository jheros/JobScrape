"""Jobicy remote-job API.

Free and keyless. ``jobGeo`` carries the hiring region ("USA", "Anywhere",
"EMEA"), which feeds the US-eligibility filter directly.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..http import Fetcher
from ..models import Job
from .base import GlobalSource, JobBatch

API = "https://jobicy.com/api/v2/remote-jobs"
COUNT = 100  # documented maximum per request

# Their industry slugs that plausibly contain software roles.
INDUSTRIES = (
    "engineering",
    "dev",
    "data-science",
    "devops-sysadmin",
    "information-security",
    "product",
)


class Jobicy(GlobalSource):
    name = "jobicy"
    note = "Free remote-job API; jobGeo states the hiring region."

    async def fetch(
        self,
        fetcher: Fetcher,
        since: date | None = None,
        until: date | None = None,
        queries: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()

        # See JobBatch — only an exhaustive crawl may be used to close postings.
        complete = True

        for industry in INDUSTRIES:
            data = await fetcher.get_json(
                API, params={"count": COUNT, "industry": industry}
            )
            if not isinstance(data, dict):
                # A whole industry silently missing from this run's results.
                complete = False
                continue
            for item in data.get("jobs") or []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                job_id = str(item["id"])
                if job_id in seen:
                    continue
                seen.add(job_id)

                job_type = item.get("jobType")
                if isinstance(job_type, list):
                    job_type = ", ".join(str(t) for t in job_type)

                def _num(value: Any) -> float | None:
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        return None

                jobs.append(
                    Job(
                        source=self.name,
                        source_job_id=job_id,
                        title=item.get("jobTitle") or "",
                        company=item.get("companyName") or "",
                        job_url=item.get("url") or "",
                        apply_url=item.get("url"),
                        location_raw=item.get("jobGeo"),
                        is_remote=True,
                        workplace_hint="remote",
                        employment_type=job_type if isinstance(job_type, str) else None,
                        department=item.get("jobIndustry")
                        if isinstance(item.get("jobIndustry"), str)
                        else "; ".join(str(i) for i in item.get("jobIndustry") or []) or None,
                        posted_at=item.get("pubDate"),
                        salary_min=_num(item.get("salaryMin")),
                        salary_max=_num(item.get("salaryMax")),
                        salary_currency=item.get("salaryCurrency"),
                        description_html=item.get("jobDescription")
                        or item.get("jobExcerpt") or None,
                        raw=item,
                    )
                )
                if max_results and len(jobs) >= max_results:
                    return JobBatch(jobs, complete=False)  # capped, not exhausted
        return JobBatch(jobs, complete=complete)
