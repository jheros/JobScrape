"""Recruitee (Tellent) public offers API.

One of the richest single-call sources available: description, requirements,
dates, structured city/country/state, salary, and separate ``remote`` /
``hybrid`` / ``on_site`` booleans — so no workplace guessing is needed.
"""

from __future__ import annotations

from typing import Any

from ..http import Fetcher
from ..models import Job
from .base import BoardSource, Prefilter

API = "https://{slug}.recruitee.com/api/offers/"


def _workplace_hint(item: dict[str, Any]) -> str | None:
    """Recruitee exposes three independent booleans; hybrid wins if set."""
    if item.get("hybrid"):
        return "hybrid"
    if item.get("remote"):
        return "remote"
    if item.get("on_site"):
        return "onsite"
    return None


class Recruitee(BoardSource):
    name = "recruitee"
    ats = "recruitee"
    note = "Public offers API; description, dates and remote/hybrid flags in one call."

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        data = await fetcher.get_json(API.format(slug=slug))
        if not isinstance(data, dict) or "offers" not in data:
            return None

        jobs: list[Job] = []
        for item in data.get("offers") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue

            locations = item.get("locations") or []
            names = [
                loc.get("name") or loc.get("city")
                for loc in locations
                if isinstance(loc, dict) and (loc.get("name") or loc.get("city"))
            ]
            location_raw = "; ".join(str(n) for n in names) or item.get("location")

            html = "\n".join(
                part for part in (item.get("description"), item.get("requirements"))
                if part
            )
            country = (item.get("country_code") or "").upper() or None

            jobs.append(
                Job(
                    source=self.name,
                    source_job_id=str(item["id"]),
                    title=item.get("title") or "",
                    company=item.get("company_name") or slug,
                    company_slug=slug,
                    job_url=item.get("careers_url") or "",
                    apply_url=item.get("careers_apply_url") or item.get("careers_url"),
                    location_raw=location_raw,
                    city=item.get("city"),
                    state=item.get("state_code") or item.get("state_name"),
                    country=country,
                    is_remote=bool(item.get("remote")) if item.get("remote") is not None else None,
                    workplace_hint=_workplace_hint(item),
                    employment_type=item.get("employment_type_code"),
                    department=item.get("department"),
                    posted_at=item.get("published_at") or item.get("created_at"),
                    updated_at=item.get("updated_at"),
                    salary_raw=item.get("salary") if isinstance(item.get("salary"), str) else None,
                    description_html=html or None,
                    raw=item if with_details else {},
                )
            )
        return jobs
