"""BambooHR public careers board.

The list call has title, location and an ``isRemote`` flag but no description or
date, so each posting needs one detail request.
"""

from __future__ import annotations

from typing import Any

from ..http import Fetcher, gather_limited
from ..models import Job
from .base import BoardSource, Prefilter, shallow_refresh

LIST = "https://{slug}.bamboohr.com/careers/list"
DETAIL = "https://{slug}.bamboohr.com/careers/{job_id}/detail"


def _location(item: dict[str, Any]) -> str | None:
    loc = item.get("location") or item.get("atsLocation") or {}
    if isinstance(loc, str):
        return loc or None
    if not isinstance(loc, dict):
        return None
    parts = [loc.get("city"), loc.get("state"), loc.get("country")]
    return ", ".join(str(p) for p in parts if p) or None


class BambooHR(BoardSource):
    name = "bamboohr"
    ats = "bamboohr"
    note = "Public careers list; description and date need one detail call each."

    async def _detail(self, fetcher: Fetcher, slug: str, job_id: str) -> dict[str, Any]:
        data = await fetcher.get_json(DETAIL.format(slug=slug, job_id=job_id))
        if not isinstance(data, dict):
            return {}
        # The payload nests the useful part two levels down.
        return ((data.get("result") or {}).get("jobOpening")) or {}

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        known_ids: set[str] | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        data = await fetcher.get_json(LIST.format(slug=slug))
        if not isinstance(data, dict) or "result" not in data:
            return None

        candidates = [
            i for i in (data.get("result") or [])
            if isinstance(i, dict) and i.get("id")
        ]
        if prefilter is not None:
            candidates = [
                i for i in candidates
                if prefilter(i.get("jobOpeningName"), i.get("departmentLabel"), _location(i))
            ]

        # Ids are stored slug-scoped, so compare in that same shape.
        known = known_ids or set()
        needs_detail = [i for i in candidates if f"{slug}:{i['id']}" not in known]

        details: dict[str, dict[str, Any]] = {}
        if with_details and needs_detail:
            fetched = await gather_limited(
                [self._detail(fetcher, slug, str(i["id"])) for i in needs_detail], limit=4
            )
            for item, result in zip(needs_detail, fetched):
                if isinstance(result, dict) and result:
                    details[str(item["id"])] = result

        jobs: list[Job] = []
        for item in candidates:
            job_id = str(item["id"])
            detail = details.get(job_id, {})
            is_remote = item.get("isRemote")
            location_type = str(item.get("locationType") or "")

            job = Job(
                    source=self.name,
                    source_job_id=f"{slug}:{job_id}",
                    title=item.get("jobOpeningName") or detail.get("jobOpeningName") or "",
                    company=slug,
                    company_slug=slug,
                    job_url=detail.get("jobOpeningShareUrl")
                    or f"https://{slug}.bamboohr.com/careers/{job_id}",
                    apply_url=f"https://{slug}.bamboohr.com/careers/{job_id}",
                    location_raw=_location(item) or _location(detail),
                    is_remote=bool(is_remote) if is_remote is not None else None,
                    workplace_hint="remote" if is_remote else (location_type or None),
                    employment_type=item.get("employmentStatusLabel"),
                    department=item.get("departmentLabel") or detail.get("departmentLabel"),
                    posted_at=detail.get("datePosted")
                    or detail.get("postedDate")
                    or detail.get("createdDate"),
                    description_html=detail.get("description")
                    or detail.get("jobOpeningDescription"),
                    raw=detail or item if with_details else {},
            )
            if f"{slug}:{job_id}" in known:
                job = shallow_refresh(job)
            jobs.append(job)
        return jobs
