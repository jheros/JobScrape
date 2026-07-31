"""Remotive remote-job API.

Free and keyless. `candidate_required_location` is the important field: it states
which regions a remote role can hire in, which is exactly the signal
:func:`jobscrape.filters.is_us_eligible` needs to tell "Remote (USA)" from
"Remote (EMEA only)".

Their response embeds a legal notice asking that the API not be redistributed
wholesale; :data:`ATTRIBUTION` is surfaced by the exporter the same way RemoteOK's
is.
"""

from __future__ import annotations

from datetime import date

from ..http import Fetcher
from ..models import Job
from .base import GlobalSource, JobBatch

API = "https://remotive.com/api/remote-jobs"
ATTRIBUTION = "Job data from Remotive (https://remotive.com)"


class Remotive(GlobalSource):
    name = "remotive"
    note = "Free remote-job API. Attribution requested; states US eligibility."

    async def fetch(
        self,
        fetcher: Fetcher,
        since: date | None = None,
        until: date | None = None,
        queries: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[Job]:
        # One request only: the API ignores both `category` and `limit` and always
        # returns the same feed (verified — six category slugs each returned the
        # identical 35 records), so a per-category sweep is pure waste.
        data = await fetcher.get_json(API)
        if not isinstance(data, dict):
            return JobBatch()  # feed failed; completeness unknown

        jobs: list[Job] = []
        seen: set[str] = set()
        for item in data.get("jobs") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            job_id = str(item["id"])
            if job_id in seen:
                continue
            seen.add(job_id)

            tags = item.get("tags") or []
            jobs.append(
                Job(
                    source=self.name,
                    source_job_id=job_id,
                    title=item.get("title") or "",
                    company=item.get("company_name") or "",
                    job_url=item.get("url") or "",
                    apply_url=item.get("url"),
                    # The hiring-region statement, not an office address.
                    location_raw=item.get("candidate_required_location"),
                    is_remote=True,
                    workplace_hint="remote",
                    employment_type=item.get("job_type"),
                    department=item.get("category")
                    or (", ".join(str(t) for t in tags[:4]) or None),
                    posted_at=item.get("publication_date"),
                    salary_raw=item.get("salary") or None,
                    description_html=item.get("description") or None,
                    raw=item,
                )
            )
            if max_results and len(jobs) >= max_results:
                # Capped, not exhausted — see JobBatch.
                return JobBatch(jobs, complete=False)
        # One request covers this source's whole feed, so reaching here means
        # every posting it lists was read.
        return JobBatch(jobs, complete=True)
