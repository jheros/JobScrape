"""RemoteOK public feed.

Their API terms ask consumers to credit RemoteOK and link back; the legal
notice they ship in element 0 of the response is preserved in
:data:`ATTRIBUTION` and surfaced by the exporter.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..http import Fetcher
from ..models import Job
from .base import GlobalSource, JobBatch

API = "https://remoteok.com/api"
ATTRIBUTION = "Job data from RemoteOK (https://remoteok.com)"


class RemoteOK(GlobalSource):
    name = "remoteok"
    note = "Public feed. ToS requires attribution and a backlink to RemoteOK."

    async def fetch(
        self,
        fetcher: Fetcher,
        since: date | None = None,
        until: date | None = None,
        queries: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[Job]:
        data = await fetcher.get_json(API, headers={"User-Agent": "Mozilla/5.0"})
        if not isinstance(data, list):
            return JobBatch()  # feed failed; completeness unknown

        jobs: list[Job] = []
        for item in data:
            # Element 0 is the legal/attribution notice, not a posting.
            if not isinstance(item, dict) or "legal" in item or not item.get("id"):
                continue
            tags = item.get("tags") or []
            location = item.get("location") or ""
            jobs.append(
                Job(
                    source=self.name,
                    source_job_id=str(item["id"]),
                    title=item.get("position") or item.get("title") or "",
                    company=item.get("company") or "",
                    job_url=item.get("url") or "",
                    apply_url=item.get("apply_url") or item.get("url"),
                    location_raw=location or "Remote",
                    is_remote=True,
                    workplace_hint="remote",  # the whole board is remote-only
                    department=", ".join(str(t) for t in tags[:5]) or None,
                    posted_at=item.get("date") or item.get("epoch"),
                    salary_min=item.get("salary_min") or None,
                    salary_max=item.get("salary_max") or None,
                    salary_currency="USD" if item.get("salary_min") else None,
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
