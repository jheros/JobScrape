"""UKG Pro Recruiting (formerly UltiPro) job boards.

The search endpoint returns everything the listing needs *including a
description*, so unlike most enterprise ATSes this needs no per-posting detail
call — one POST per page covers the whole board.

A board is ``<host>/<tenant>/<guid>``, e.g.
``accessiblespace.rec.pro.ukg.net/acc1507asei/1f3e7f92-...``. The host carries
the data-residency TLD, so it cannot be assumed.
"""

from __future__ import annotations

from typing import Any

from ..http import Fetcher
from ..models import Job, html_to_text
from .base import BoardSource, Prefilter, shallow_refresh

SEARCH = "https://{host}/{tenant}/JobBoard/{guid}/JobBoardView/LoadSearchResults"
DETAIL_URL = "https://{host}/{tenant}/JobBoard/{guid}/OpportunityDetail?opportunityId={job_id}"
PAGE_SIZE = 50
MAX_PER_BOARD = 2000


def parse_slug(slug: str) -> tuple[str, str, str] | None:
    """Split a ``host/tenant/guid`` triple."""
    parts = slug.strip().split("/")
    if len(parts) == 3 and all(parts):
        return parts[0], parts[1], parts[2]
    return None


def _location(item: dict[str, Any]) -> str | None:
    """Flatten UKG's nested location list into a display string."""
    locations = item.get("Locations") or []
    if not isinstance(locations, list):
        return None
    parts: list[str] = []
    for loc in locations[:3]:
        if not isinstance(loc, dict):
            continue
        address = loc.get("Address") or {}
        if not isinstance(address, dict):
            continue
        city = (address.get("City") or {}).get("Name") if isinstance(address.get("City"), dict) else None
        state = (address.get("State") or {}).get("Code") if isinstance(address.get("State"), dict) else None
        country = (address.get("Country") or {}).get("Code") if isinstance(address.get("Country"), dict) else None
        piece = ", ".join(str(p) for p in (city, state, country) if p)
        if piece:
            parts.append(piece)
    return "; ".join(dict.fromkeys(parts)) or None


def _location_parts(item: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """The first location's structured ``(city, state, country)``.

    `_location` above walks exactly these fields and joins them into a display
    string; the parts never reached the `Job`, so they were re-derived from the
    string built out of them. The FIRST location only — a posting listing three
    offices has one `city` column to put them in, and `location_raw` already
    carries all three.
    """
    locations = item.get("Locations") or []
    if not isinstance(locations, list):
        return None, None, None
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        address = loc.get("Address") or {}
        if not isinstance(address, dict):
            continue

        def code(key: str, field: str) -> str | None:
            holder = address.get(key)
            value = holder.get(field) if isinstance(holder, dict) else None
            return str(value) if value else None

        city = code("City", "Name")
        state = code("State", "Code")
        country = code("Country", "Code")
        if city or state or country:
            return city, state, country
    return None, None, None


class UKG(BoardSource):
    name = "ukg"
    ats = "ukg"
    note = "UKG Pro Recruiting search API; the listing already carries the description."

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        known_ids: set[str] | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        parsed = parse_slug(slug)
        if not parsed:
            return None
        host, tenant, guid = parsed
        url = SEARCH.format(host=host, tenant=tenant, guid=guid)

        postings: list[dict[str, Any]] = []
        skip = 0
        total: int | None = None
        while skip < MAX_PER_BOARD:
            data = await fetcher.post_json(
                url,
                {"opportunitySearch": {"Top": PAGE_SIZE, "Skip": skip}},
                headers={"content-type": "application/json", "accept": "application/json"},
            )
            if not isinstance(data, dict):
                # First-page failure means the board is gone; a later one is a
                # partial result, not a dead board.
                if skip == 0:
                    return None
                break
            batch = data.get("opportunities") or []
            if total is None:
                total = int(data.get("totalCount") or 0)
            if not batch:
                break
            postings.extend(b for b in batch if isinstance(b, dict))
            skip += PAGE_SIZE
            if len(batch) < PAGE_SIZE or (total and skip >= total):
                break
            if not with_details:
                break  # probing only needs to prove the board answers

        if not postings:
            # `[]`, not `None`, on BOTH paths. By this point the API has
            # answered — a genuinely dead board already returned `None` from the
            # first-page guard above — so zero postings means "exists, not
            # hiring". `BoardSource.probe` maps `None` to "dead" and `[]` to
            # "empty", and `dead` boards are never selected for a fetch again
            # (`_fetch_boards` asks for `status="live"`), so returning `None`
            # here retired live employers on the day they happened to have no
            # openings. Measured: Workday had 0 empty boards out of 83 while
            # every other ATS ran 7-48%.
            return []

        if prefilter is not None:
            postings = [
                p for p in postings
                if prefilter(p.get("Title"), p.get("JobCategoryName"), _location(p))
            ]

        known = known_ids or set()
        jobs: list[Job] = []
        for item in postings:
            job_id = str(item.get("Id") or "")
            if not job_id:
                continue
            body = item.get("BriefDescription")
            remote = str(item.get("JobLocationType") or "").lower() or None
            _city, _state, _country = _location_parts(item)
            job = Job(
                source=self.name,
                source_job_id=f"{tenant}:{job_id}",
                title=item.get("Title") or "",
                # `tenant` is UKG's opaque tenant id, not the employer's real name
                # — the blocklist in cli.py's _fetch_boards can't match this board
                # to a blocked employer by name alone. Recovering the real name
                # would need it parsed out of the UKG API response.
                company=tenant,
                company_slug=slug,
                job_url=DETAIL_URL.format(host=host, tenant=tenant, guid=guid, job_id=job_id),
                location_raw=_location(item),
                city=_city, state=_state, country=_country,
                workplace_hint=remote,
                employment_type="full_time" if item.get("FullTime") else None,
                department=item.get("JobCategoryName"),
                posted_at=item.get("PostedDate"),
                description_html=body,
                description_text=html_to_text(body) or None,
                raw=item,
            )
            if job_id in known:
                job = shallow_refresh(job)
            jobs.append(job)
        return jobs
