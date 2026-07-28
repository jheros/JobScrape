"""Workable job boards (public widget API)."""

from __future__ import annotations

from typing import Any

from ..http import Fetcher
from ..models import Job
from .base import BoardSource

ACCOUNT = "https://apply.workable.com/api/v1/widget/accounts/{slug}"


class Workable(BoardSource):
    name = "workable"
    ats = "workable"
    note = "Public widget API; ?details=true carries descriptions."

    async def fetch_board(
        self, fetcher: Fetcher, slug: str, with_details: bool = True, **kw: Any
    ) -> list[Job] | None:
        data = await fetcher.get_json(
            ACCOUNT.format(slug=slug),
            params={"details": "true"} if with_details else None,
        )
        if not isinstance(data, dict) or "jobs" not in data:
            return None

        company = data.get("name") or slug

        # Workable repeats one requisition once per location, so the same
        # shortcode arrives several times with different cities. Collapse them
        # into a single row and keep every location rather than letting the
        # last copy silently overwrite the others.
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in data.get("jobs") or []:
            if not isinstance(item, dict):
                continue
            shortcode = item.get("shortcode") or item.get("id")
            if not shortcode:
                continue
            grouped.setdefault(str(shortcode), []).append(item)

        jobs: list[Job] = []
        for shortcode, copies in grouped.items():
            item = copies[0]
            locations: list[str] = []
            for copy in copies:
                parts = [
                    copy.get("city"),
                    copy.get("state") or copy.get("region"),
                    copy.get("country"),
                ]
                text = ", ".join(str(p) for p in parts if p)
                if text and text not in locations:
                    locations.append(text)

            city = item.get("city")
            state = item.get("state") or item.get("region")
            location_raw = "; ".join(locations)
            telecommuting = item.get("telecommuting")

            description = item.get("description") or ""
            requirements = item.get("requirements") or ""
            benefits = item.get("benefits") or ""
            html = "\n".join(p for p in (description, requirements, benefits) if p)

            jobs.append(
                Job(
                    source=self.name,
                    # Scoped by account: Workable shortcodes are only
                    # guaranteed unique within one company's board.
                    source_job_id=f"{slug}:{shortcode}",
                    title=item.get("title") or "",
                    company=company,
                    company_slug=slug,
                    job_url=item.get("url") or f"https://apply.workable.com/{slug}/j/{shortcode}/",
                    apply_url=item.get("application_url") or item.get("url"),
                    location_raw=location_raw or None,
                    city=city,
                    state=state,
                    country=(item.get("country_code") or "").upper() or None,
                    is_remote=bool(telecommuting) if telecommuting is not None else None,
                    workplace_hint=(
                        "remote" if telecommuting
                        else "onsite" if telecommuting is False else None
                    ),
                    employment_type=item.get("employment_type"),
                    department=item.get("department") or item.get("function"),
                    posted_at=item.get("published_on") or item.get("created_at"),
                    description_html=html or None,
                    raw=item if with_details else {},
                )
            )
        return jobs
