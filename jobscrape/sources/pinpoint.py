"""Pinpoint public postings API.

Single call, and the only source that ships its own ``workplace_type`` field
(``remote`` / ``hybrid`` / ``on_site``) — no inference required.
"""

from __future__ import annotations

import re
from typing import Any

from ..http import Fetcher, gather_limited
from ..models import Job
from .base import BoardSource, Prefilter, shallow_refresh

API = "https://{slug}.pinpointhq.com/postings.json"

# The JSON feed carries no posting date — only `deadline_at`. The date lives in
# the schema.org JSON-LD on the posting page, so a dated query needs one HTML
# request per posting. Without it every Pinpoint row would be undated and
# therefore dropped by any --since/--until window.
_DATE_POSTED_RE = re.compile(r'"datePosted"\s*:\s*"([^"]+)"')


def _description(item: dict[str, Any]) -> str:
    """Pinpoint splits a posting across several labelled HTML blocks."""
    parts: list[str] = []
    for header_key, body_key in (
        (None, "description"),
        ("key_responsibilities_header", "key_responsibilities"),
        ("skills_knowledge_expertise_header", "skills_knowledge_expertise"),
        ("benefits_header", "benefits"),
    ):
        body = item.get(body_key)
        if not body:
            continue
        header = item.get(header_key) if header_key else None
        if header:
            parts.append(f"<h3>{header}</h3>")
        parts.append(str(body))
    return "\n".join(parts)


class Pinpoint(BoardSource):
    name = "pinpoint"
    ats = "pinpoint"
    note = "Public postings API; ships a native workplace_type field."

    async def _posted_at(self, fetcher: Fetcher, url: str | None) -> str | None:
        """Pull `datePosted` out of the posting page's schema.org block."""
        if not url:
            return None
        html = await fetcher.get_text(url, retries=1)
        if not html:
            return None
        match = _DATE_POSTED_RE.search(html)
        return match.group(1) if match else None

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
        if not isinstance(data, dict) or "data" not in data:
            return None

        candidates = [
            i for i in (data.get("data") or [])
            if isinstance(i, dict) and i.get("id")
        ]
        if prefilter is not None:
            candidates = [
                i for i in candidates
                if prefilter(
                    i.get("title"),
                    ((i.get("job") or {}).get("department") or {}).get("name"),
                    (i.get("location") or {}).get("city")
                    or (i.get("location") or {}).get("name"),
                )
            ]

        # A stored posting already has its date, so skip the page fetch.
        known = known_ids or set()
        needs_detail = [i for i in candidates if str(i["id"]) not in known]

        dates: dict[str, str] = {}
        if with_details and needs_detail:
            fetched = await gather_limited(
                [self._posted_at(fetcher, i.get("url")) for i in needs_detail], limit=4
            )
            for item, posted in zip(needs_detail, fetched):
                if posted:
                    dates[str(item["id"])] = posted

        jobs: list[Job] = []
        for item in candidates:
            loc = item.get("location") or {}
            job_meta = item.get("job") or {}
            department = (job_meta.get("department") or {}).get("name")
            location_raw = ", ".join(
                str(p) for p in (
                    loc.get("city") or loc.get("name"),
                    loc.get("province"),
                    loc.get("country") or loc.get("country_name"),
                ) if p
            ) or None

            def _num(value: Any) -> float | None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None

            job = Job(
                    source=self.name,
                    source_job_id=str(item["id"]),
                    title=item.get("title") or "",
                    company=slug,
                    company_slug=slug,
                    job_url=item.get("url") or "",
                    apply_url=item.get("url"),
                    location_raw=location_raw,
                    city=loc.get("city"),
                    state=loc.get("province"),
                    # Native field: "remote" / "hybrid" / "on_site".
                    workplace_hint=item.get("workplace_type")
                    or item.get("workplace_type_text"),
                    employment_type=item.get("employment_type_text")
                    or item.get("employment_type"),
                    department=department,
                    posted_at=dates.get(str(item["id"]))
                    or item.get("published_at")
                    or item.get("created_at"),
                    salary_min=_num(item.get("compensation_minimum")),
                    salary_max=_num(item.get("compensation_maximum")),
                    salary_currency=item.get("compensation_currency"),
                    salary_raw=item.get("compensation")
                    if isinstance(item.get("compensation"), str) else None,
                    description_html=_description(item) or None,
                    raw=item if with_details else {},
            )
            if str(item["id"]) in known:
                job = shallow_refresh(job)
            jobs.append(job)
        return jobs
