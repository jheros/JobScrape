"""Teamtailor public board, served as a JSON Feed.

Each item carries ``content_html`` (the full posting) and ``date_published``, and
embeds a schema.org ``JobPosting`` under ``_jobposting`` — which is where the
location and employment type live.
"""

from __future__ import annotations

from typing import Any

from ..http import Fetcher
from ..models import Job
from .base import BoardSource, Prefilter

API = "https://{slug}.teamtailor.com/jobs.json"


def _location_from_schema(posting: dict[str, Any]) -> str | None:
    """Flatten schema.org ``jobLocation`` into a readable string.

    It may be a single object or a list, and the useful parts sit two levels
    down under ``address``.
    """
    raw = posting.get("jobLocation")
    if not raw:
        return None
    entries = raw if isinstance(raw, list) else [raw]
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address")
        if isinstance(address, dict):
            parts = [
                address.get("addressLocality"),
                address.get("addressRegion"),
                address.get("addressCountry"),
            ]
            text = ", ".join(str(p) for p in parts if p)
        else:
            text = str(entry.get("name") or "")
        if text and text not in names:
            names.append(text)
    return "; ".join(names) or None


class Teamtailor(BoardSource):
    name = "teamtailor"
    ats = "teamtailor"
    note = "JSON Feed board; full description plus embedded schema.org JobPosting."

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        data = await fetcher.get_json(API.format(slug=slug))
        if not isinstance(data, dict) or "items" not in data:
            return None

        company = data.get("title") or slug
        jobs: list[Job] = []
        for item in data.get("items") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            posting = item.get("_jobposting") or {}
            if not isinstance(posting, dict):
                posting = {}

            hiring_org = posting.get("hiringOrganization")
            org_name = (
                hiring_org.get("name") if isinstance(hiring_org, dict) else None
            )
            employment = posting.get("employmentType")
            if isinstance(employment, list):
                employment = ", ".join(str(e) for e in employment)

            jobs.append(
                Job(
                    source=self.name,
                    source_job_id=str(item["id"]),
                    title=item.get("title") or posting.get("title") or "",
                    company=org_name or company,
                    company_slug=slug,
                    job_url=item.get("url") or "",
                    apply_url=(posting.get("directApply") and item.get("url"))
                    or item.get("url"),
                    location_raw=_location_from_schema(posting),
                    # schema.org marks remote roles with this flag.
                    workplace_hint="remote"
                    if posting.get("jobLocationType") == "TELECOMMUTE"
                    else None,
                    employment_type=employment,
                    department=(posting.get("occupationalCategory") or None),
                    posted_at=item.get("date_published") or posting.get("datePosted"),
                    description_html=item.get("content_html")
                    or posting.get("description"),
                    raw=item if with_details else {},
                )
            )
        return jobs
