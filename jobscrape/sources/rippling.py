"""Rippling ATS public job boards.

A clean public REST API: one call lists a board, one call per posting supplies
the description, pay range and dates. Rippling is common among US startups and
mid-market companies.

Substituted for Paycom during this work: Paycom's API still exists but now
gates every call behind a token that lives inside a minified JS bundle, so
reaching it means reverse-engineering that bundle and re-doing it on their next
deploy. Rippling gives comparable US coverage from a documented public shape.
"""

from __future__ import annotations

from typing import Any

from ..http import Fetcher, gather_limited
from ..models import Job, html_to_text
from .base import BoardSource, Prefilter, shallow_refresh

LIST = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"
DETAIL = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{job_id}"


def _label(value: Any) -> str | None:
    """Rippling wraps enums as ``{"id": ..., "label": ...}``."""
    if isinstance(value, dict):
        return value.get("label") or value.get("id")
    return str(value) if value else None


def _locations(detail: dict[str, Any], listed: dict[str, Any]) -> str | None:
    places = detail.get("workLocations")
    if isinstance(places, list) and places:
        labels = [_label(p) for p in places[:4]]
        return "; ".join(dict.fromkeys(l for l in labels if l)) or None
    return _label(listed.get("workLocation"))


def _description(detail: dict[str, Any]) -> str | None:
    """Join Rippling's sectioned description into one HTML body.

    ``description`` is an object of named sections (``company``, ``role``, …),
    not a string — passing it straight to the HTML flattener raises. Sections
    are joined in payload order so the role text keeps its position relative to
    the company boilerplate.
    """
    body = detail.get("description")
    if isinstance(body, str):
        return body or None
    if isinstance(body, dict):
        parts = [str(v) for v in body.values() if isinstance(v, str) and v.strip()]
        return "\n".join(parts) or None
    return None


def _pay(detail: dict[str, Any]) -> tuple[float | None, float | None, str | None]:
    band = detail.get("payRangeDetails")
    # An unset band is served as an empty LIST, not an empty object.
    if not isinstance(band, dict):
        return None, None, None

    def _num(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return _num(band.get("min")), _num(band.get("max")), band.get("currency") or None


class Rippling(BoardSource):
    name = "rippling"
    ats = "rippling"
    note = "Public REST board; one detail call per posting for the description."

    async def _detail(
        self, fetcher: Fetcher, slug: str, job_id: str
    ) -> dict[str, Any] | None:
        data = await fetcher.get_json(DETAIL.format(slug=slug, job_id=job_id))
        return data if isinstance(data, dict) else None

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
        if not isinstance(data, list):
            return None
        postings = [p for p in data if isinstance(p, dict) and p.get("uuid")]
        if not postings:
            return []

        if prefilter is not None:
            postings = [
                p for p in postings
                if prefilter(p.get("name"), _label(p.get("department")),
                             _label(p.get("workLocation")))
            ]
            if not postings:
                return []

        known = known_ids or set()
        details: dict[str, dict[str, Any]] = {}
        if with_details:
            needed = [p["uuid"] for p in postings if p["uuid"] not in known]
            fetched = await gather_limited(
                [self._detail(fetcher, slug, j) for j in needed], limit=4
            )
            for job_id, result in zip(needed, fetched, strict=True):
                if isinstance(result, dict):
                    details[job_id] = result

        jobs: list[Job] = []
        for item in postings:
            job_id = str(item["uuid"])
            detail = details.get(job_id, {})
            body = _description(detail)
            lo, hi, currency = _pay(detail)
            job = Job(
                source=self.name,
                source_job_id=f"{slug}:{job_id}",
                title=item.get("name") or detail.get("name") or "",
                company=detail.get("companyName") or slug,
                company_slug=slug,
                job_url=item.get("url") or detail.get("url") or "",
                location_raw=_locations(detail, item),
                employment_type=_label(detail.get("employmentType")),
                department=_label(item.get("department")),
                posted_at=detail.get("createdOn"),
                salary_min=lo,
                salary_max=hi,
                salary_currency=currency,
                description_html=body,
                description_text=html_to_text(body) or None,
                raw=detail or item,
            )
            if job_id in known:
                job = shallow_refresh(job)
            jobs.append(job)
        return jobs
