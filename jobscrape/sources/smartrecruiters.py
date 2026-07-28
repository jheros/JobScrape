"""SmartRecruiters public postings API.

Best structured location data of any source: city, region, country and explicit
remote/hybrid booleans, so no string parsing is needed.
"""

from __future__ import annotations

from typing import Any

from ..http import Fetcher, gather_limited
from ..models import Job, html_to_text
from .base import BoardSource, Prefilter, shallow_refresh

LIST = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
DETAIL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}"
PAGE_SIZE = 100


def _sections_to_html(detail: dict[str, Any]) -> str:
    """Flatten the nested jobAd section structure into one HTML blob."""
    ad = (detail.get("jobAd") or {}).get("sections") or {}
    parts: list[str] = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        section = ad.get(key) or {}
        title = section.get("title") or ""
        text = section.get("text") or ""
        if text:
            if title:
                parts.append(f"<h3>{title}</h3>")
            parts.append(text)
    return "\n".join(parts)


class SmartRecruiters(BoardSource):
    name = "smartrecruiters"
    ats = "smartrecruiters"
    note = "Public API with fully structured city/region/country + remote flags."

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        known_ids: set[str] | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        postings: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await fetcher.get_json(
                LIST.format(slug=slug),
                params={"limit": PAGE_SIZE, "offset": offset},
            )
            if not isinstance(page, dict) or "content" not in page:
                # A failure on the very first page means the board doesn't
                # exist; later failures just end pagination with what we have.
                if offset == 0:
                    return None
                break
            batch = page.get("content") or []
            postings.extend(b for b in batch if isinstance(b, dict))
            offset += PAGE_SIZE
            if len(batch) < PAGE_SIZE or offset >= int(page.get("totalFound") or 0):
                break
            if offset > 5000:  # safety valve on very large employers
                break

        # Skip detail lookups for postings that clearly will not survive
        # filtering; the listing already has title, department and location.
        candidates = [p for p in postings if p.get("id")]
        if prefilter is not None:
            candidates = [
                p for p in candidates
                if prefilter(
                    p.get("name"),
                    (p.get("department") or {}).get("label")
                    or (p.get("function") or {}).get("label"),
                    (p.get("location") or {}).get("fullLocation"),
                    # Structured country: lets a US posting survive even when
                    # the location string names a non-US city.
                    ((p.get("location") or {}).get("country") or "").upper() or None,
                )
            ]

        # Skip the detail request for postings already stored; ids here are
        # bare, matching how source_job_id is built below.
        known = known_ids or set()
        needs_detail = [p for p in candidates if str(p["id"]) not in known]

        details: dict[str, dict[str, Any]] = {}
        if with_details and needs_detail:
            fetched = await gather_limited(
                [
                    fetcher.get_json(DETAIL.format(slug=slug, job_id=p["id"]))
                    for p in needs_detail
                ],
                limit=6,
            )
            for result in fetched:
                if isinstance(result, dict) and result.get("id"):
                    # Key as str to match the lookup in _build; a type mismatch
                    # here would silently drop every description.
                    details[str(result["id"])] = result

        return self._build(slug, candidates, details, known)

    def _build(
        self,
        slug: str,
        postings: list[dict[str, Any]],
        details: dict[str, dict[str, Any]],
        known: set[str] | None = None,
    ) -> list[Job]:
        jobs: list[Job] = []
        for item in postings:
            job_id = str(item.get("id") or "")
            if not job_id:
                continue
            detail = details.get(job_id, {})
            loc = item.get("location") or detail.get("location") or {}
            company_info = item.get("company") or detail.get("company") or {}
            company = company_info.get("name") or slug
            html = _sections_to_html(detail)
            remote = loc.get("remote")

            # The list response's `ref` is an API endpoint, not something a
            # person can open. Only the detail response carries the public URLs,
            # so fall back to constructing one from the company identifier
            # (which is case-sensitive, unlike our lowercase slug).
            identifier = company_info.get("identifier") or slug
            public_url = detail.get("postingUrl") or (
                f"https://jobs.smartrecruiters.com/{identifier}/{job_id}"
            )
            apply_url = detail.get("applyUrl") or public_url

            city, region = loc.get("city"), loc.get("region")
            country = (loc.get("country") or "").upper() or None
            location_raw = loc.get("fullLocation") or ", ".join(
                p for p in (city, region, country) if p
            )

            job = Job(
                    source=self.name,
                    source_job_id=job_id,
                    title=item.get("name") or "",
                    company=company,
                    company_slug=slug,
                    job_url=public_url,
                    apply_url=apply_url,
                    location_raw=location_raw or None,
                    city=city,
                    state=region,
                    country=country,
                    is_remote=bool(remote) if remote is not None else None,
                    # Structured booleans; hybrid must be checked before remote
                    # because a posting can set both.
                    workplace_hint=(
                        "hybrid" if loc.get("hybrid") else
                        "remote" if remote else
                        "onsite" if remote is False else None
                    ),
                    employment_type=(item.get("typeOfEmployment") or {}).get("label"),
                    department=(item.get("department") or {}).get("label")
                    or (item.get("function") or {}).get("label"),
                    posted_at=item.get("releasedDate") or detail.get("releasedDate"),
                    description_html=html or None,
                    description_text=html_to_text(html) or None,
                    raw=detail or item if details else {},
            )
            if known and job_id in known:
                job = shallow_refresh(job)
            jobs.append(job)
        return jobs
