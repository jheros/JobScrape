"""Breezy HR public board.

The list call gives title, date, structured location and type but no
description, so the posting body needs one detail request each.
"""

from __future__ import annotations

from typing import Any

from ..http import Fetcher, gather_limited
from ..models import Job
from . import jsonld
from .base import BoardSource, Prefilter, shallow_refresh

API = "https://{slug}.breezy.hr/json"


def _location(item: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    """Return ``(display, city, state, country_code)`` from Breezy's nested shape."""
    loc = item.get("location") or {}
    if not isinstance(loc, dict):
        return None, None, None, None
    city = loc.get("city")
    state_obj = loc.get("state") or {}
    country_obj = loc.get("country") or {}
    state = state_obj.get("id") or state_obj.get("name") if isinstance(state_obj, dict) else None
    country_name = country_obj.get("name") if isinstance(country_obj, dict) else None
    country_code = country_obj.get("id") if isinstance(country_obj, dict) else None
    display = ", ".join(str(p) for p in (city, state, country_name) if p) or None
    return display, city, state, (country_code or None)


class Breezy(BoardSource):
    name = "breezy"
    ats = "breezy"
    note = "Public JSON board; description needs one detail call per posting."

    async def _detail(self, fetcher: Fetcher, url: str | None) -> dict[str, Any] | None:
        """Breezy has no JSON detail endpoint, but the posting page embeds
        schema.org JSON-LD carrying the full description."""
        if not url:
            return None
        html = await fetcher.get_text(url, retries=1)
        return jsonld.first_job_posting(html)

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        known_ids: set[str] | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        data = await fetcher.get_json(API.format(slug=slug))
        if not isinstance(data, list):
            return None

        candidates = [i for i in data if isinstance(i, dict) and i.get("id")]
        if prefilter is not None:
            kept = []
            for item in candidates:
                display, _, _, country = _location(item)
                dept = item.get("department")
                if isinstance(dept, dict):
                    dept = dept.get("name")
                if prefilter(item.get("name"), dept, display, country):
                    kept.append(item)
            candidates = kept

        known = known_ids or set()
        needs_detail = [i for i in candidates if f"{slug}:{i['id']}" not in known]

        details: dict[str, dict[str, Any]] = {}
        if with_details and needs_detail:
            fetched = await gather_limited(
                [self._detail(fetcher, i.get("url")) for i in needs_detail], limit=4
            )
            for item, result in zip(needs_detail, fetched):
                if isinstance(result, dict):
                    details[str(item["id"])] = result

        jobs: list[Job] = []
        for item in candidates:
            job_id = str(item["id"])
            detail = details.get(job_id, {})
            display, city, state, country = _location(item)
            job_type = item.get("type")
            if isinstance(job_type, dict):
                job_type = job_type.get("name") or job_type.get("id")
            loc = item.get("location") or {}
            is_remote = loc.get("is_remote") if isinstance(loc, dict) else None
            # Several Breezy fields are objects where other boards use strings.
            company = item.get("company")
            if isinstance(company, dict):
                company = company.get("name")
            department = item.get("department")
            if isinstance(department, dict):
                department = department.get("name")

            job = Job(
                    source=self.name,
                    source_job_id=f"{slug}:{job_id}",
                    title=item.get("name") or "",
                    company=company or slug,
                    company_slug=slug,
                    job_url=item.get("url") or "",
                    apply_url=item.get("url"),
                    location_raw=display,
                    city=city,
                    state=state,
                    country=country,
                    is_remote=bool(is_remote) if is_remote is not None else None,
                    workplace_hint="remote" if is_remote else None,
                    employment_type=job_type if isinstance(job_type, str) else None,
                    department=department if isinstance(department, str) else None,
                    posted_at=item.get("published_date") or item.get("creation_date"),
                    salary_raw=item.get("salary") if isinstance(item.get("salary"), str) else None,
                    description_html=jsonld.description(detail),
                    raw=detail or item if with_details else {},
            )
            if f"{slug}:{job_id}" in known:
                job = shallow_refresh(job)
            jobs.append(job)
        return jobs
