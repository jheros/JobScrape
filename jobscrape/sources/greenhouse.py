"""Greenhouse job boards.

The friendliest source available: ``?content=true`` returns every open posting
*with* its full description in a single request, so one call per company is all
we need.
"""

from __future__ import annotations

from typing import Any

from ..http import Fetcher
from ..models import Job
from .base import BoardSource, join_locations

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
BOARD = "https://boards-api.greenhouse.io/v1/boards/{slug}"


class Greenhouse(BoardSource):
    name = "greenhouse"
    ats = "greenhouse"
    note = "Public board API; full descriptions in one call."

    async def fetch_board(
        self, fetcher: Fetcher, slug: str, with_details: bool = True, **kw: Any
    ) -> list[Job] | None:
        url = API.format(slug=slug)
        params = {"content": "true"} if with_details else None
        data = await fetcher.get_json(url, params=params)
        if not isinstance(data, dict) or "jobs" not in data:
            return None

        company_name = slug
        if with_details:
            meta = await fetcher.get_json(BOARD.format(slug=slug))
            if isinstance(meta, dict) and meta.get("name"):
                company_name = meta["name"]

        jobs: list[Job] = []
        for item in data.get("jobs") or []:
            if not isinstance(item, dict):
                continue
            content = item.get("content") or ""
            offices = [o.get("name") for o in (item.get("offices") or []) if o.get("name")]
            departments = [
                d.get("name") for d in (item.get("departments") or []) if d.get("name")
            ]
            location = (item.get("location") or {}).get("name") or join_locations(offices, sep=", ")

            # Metadata is a free-form list; a pay range sometimes lives there.
            # Anything not found here is recovered by Job's description scan.
            salary_text = ""
            for field in item.get("metadata") or []:
                if isinstance(field, dict) and "salary" in str(field.get("name", "")).lower():
                    value = field.get("value")
                    if value:
                        salary_text = str(value)
                        break

            jobs.append(
                Job(
                    source=self.name,
                    source_job_id=str(item.get("id")),
                    title=item.get("title") or "",
                    company=item.get("company_name") or company_name,
                    company_slug=slug,
                    job_url=item.get("absolute_url") or "",
                    apply_url=item.get("absolute_url"),
                    location_raw=location or None,
                    department=", ".join(departments) or None,
                    posted_at=item.get("first_published") or item.get("updated_at"),
                    updated_at=item.get("updated_at"),
                    description_html=content or None,
                    salary_raw=salary_text or None,
                    raw=item if with_details else {},
                )
            )
        return jobs
