"""Lever job boards.

One call returns everything including descriptions. Note ``createdAt`` is epoch
**milliseconds** — :func:`jobscrape.models.to_utc_iso` handles the conversion.
"""

from __future__ import annotations

from typing import Any

from ..http import Fetcher
from ..models import Job
from .base import BoardSource

API = "https://api.lever.co/v0/postings/{slug}?mode=json"


def _full_description(item: dict[str, Any]) -> tuple[str, str]:
    """Reassemble Lever's split description into complete HTML and text.

    Lever splits a posting across ``description`` (intro), ``lists`` (the
    requirements/responsibilities sections) and ``additional`` (closing).
    Taking only ``descriptionPlain`` would drop most of the posting.
    """
    html_parts = [item.get("description") or ""]
    text_parts = [item.get("descriptionPlain") or ""]
    for section in item.get("lists") or []:
        if not isinstance(section, dict):
            continue
        heading = section.get("text") or ""
        content = section.get("content") or ""
        if heading:
            html_parts.append(f"<h3>{heading}</h3>")
            text_parts.append(f"\n{heading}\n")
        html_parts.append(content)
        text_parts.append(content)
    if item.get("additional"):
        html_parts.append(item["additional"])
        text_parts.append(item.get("additionalPlain") or "")
    return "\n".join(p for p in html_parts if p), "\n".join(p for p in text_parts if p)


class Lever(BoardSource):
    name = "lever"
    ats = "lever"
    note = "Public postings API; includes country and workplace type."

    async def fetch_board(
        self, fetcher: Fetcher, slug: str, with_details: bool = True, **kw: Any
    ) -> list[Job] | None:
        data = await fetcher.get_json(API.format(slug=slug))
        if not isinstance(data, list):
            return None

        jobs: list[Job] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            categories = item.get("categories") or {}
            locations = categories.get("allLocations") or []
            location = categories.get("location") or (locations[0] if locations else None)
            if len(locations) > 1:
                location = "; ".join(str(loc) for loc in locations)

            salary = item.get("salaryRange") or {}
            workplace = (item.get("workplaceType") or "").lower()
            desc_html, desc_text = _full_description(item)

            jobs.append(
                Job(
                    source=self.name,
                    source_job_id=str(item.get("id")),
                    title=item.get("text") or "",
                    company=slug,
                    company_slug=slug,
                    job_url=item.get("hostedUrl") or "",
                    apply_url=item.get("applyUrl") or item.get("hostedUrl"),
                    location_raw=location,
                    country=item.get("country"),
                    is_remote=workplace == "remote" if workplace else None,
                    # Lever states this outright: remote / hybrid / on-site.
                    workplace_hint=item.get("workplaceType"),
                    employment_type=categories.get("commitment"),
                    department=categories.get("department") or categories.get("team"),
                    posted_at=item.get("createdAt"),
                    salary_min=salary.get("min"),
                    salary_max=salary.get("max"),
                    salary_currency=salary.get("currency"),
                    description_html=desc_html or None,
                    description_text=desc_text or None,
                    raw=item if with_details else {},
                )
            )
        return jobs
