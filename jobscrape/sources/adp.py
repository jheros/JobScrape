"""ADP Workforce Now career centres.

A public JSON API: one paged listing call, then one detail call per posting for
the description. ADP hosts a large slice of US mid-market and enterprise
employers.

A board is the ``cid`` GUID from the career-centre URL, optionally with the
``ccId`` after a colon (``<cid>:<ccId>``) for tenants that need it.
"""

from __future__ import annotations

from typing import Any

from ..http import Fetcher, gather_limited
from ..models import Job, html_to_text
from .base import BoardSource, Prefilter, shallow_refresh

BASE = (
    "https://workforcenow.adp.com/mascsr/default/careercenter/public"
    "/events/staffing/v1/job-requisitions"
)
PAGE_SIZE = 50
MAX_PER_BOARD = 2000


def parse_slug(slug: str) -> tuple[str, str | None]:
    """Split ``cid`` or ``cid:ccId``."""
    parts = slug.strip().split(":", 1)
    return parts[0], (parts[1] if len(parts) == 2 and parts[1] else None)


def _location(item: dict[str, Any]) -> str | None:
    """Flatten ADP's deeply nested address shape."""
    places = item.get("requisitionLocations") or []
    if not isinstance(places, list):
        return None
    out: list[str] = []
    for place in places[:3]:
        address = (place or {}).get("address") or {}
        if not isinstance(address, dict):
            continue
        city = address.get("cityName")
        region = (address.get("countrySubdivisionLevel1") or {})
        state = region.get("codeValue") if isinstance(region, dict) else None
        country = (address.get("countryCode") or {})
        country_code = country.get("codeValue") if isinstance(country, dict) else country
        piece = ", ".join(str(p) for p in (city, state, country_code) if p)
        if piece:
            out.append(piece)
    return "; ".join(dict.fromkeys(out)) or None


class ADP(BoardSource):
    name = "adp"
    ats = "adp"
    note = "ADP Workforce Now public API; detail call per posting for the description."

    async def _detail(
        self, fetcher: Fetcher, cid: str, job_id: str
    ) -> dict[str, Any] | None:
        data = await fetcher.get_json(
            f"{BASE}/{job_id}", params={"cid": cid},
            headers={"Accept": "application/json"},
        )
        if not isinstance(data, dict):
            return None
        items = data.get("jobRequisitions")
        if isinstance(items, list) and items:
            return items[0] if isinstance(items[0], dict) else None
        return data

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        known_ids: set[str] | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        cid, cc_id = parse_slug(slug)
        if not cid:
            return None

        postings: list[dict[str, Any]] = []
        skip = 0
        while skip < MAX_PER_BOARD:
            params: dict[str, Any] = {"cid": cid, "$top": PAGE_SIZE, "$skip": skip}
            if cc_id:
                params["ccId"] = cc_id
            data = await fetcher.get_json(
                BASE, params=params, headers={"Accept": "application/json"}
            )
            if not isinstance(data, dict):
                if skip == 0:
                    return None
                break
            batch = data.get("jobRequisitions") or []
            if not batch:
                break
            postings.extend(b for b in batch if isinstance(b, dict))
            skip += PAGE_SIZE
            if len(batch) < PAGE_SIZE:
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
                if prefilter(p.get("requisitionTitle"), None, _location(p))
            ]
            if not postings:
                return []

        known = known_ids or set()
        details: dict[str, dict[str, Any]] = {}
        if with_details:
            needed = [str(p.get("itemID")) for p in postings
                      if p.get("itemID") and str(p["itemID"]) not in known]
            fetched = await gather_limited(
                [self._detail(fetcher, cid, j) for j in needed], limit=4
            )
            for job_id, result in zip(needed, fetched, strict=True):
                if isinstance(result, dict):
                    details[job_id] = result

        jobs: list[Job] = []
        for item in postings:
            job_id = str(item.get("itemID") or "")
            if not job_id:
                continue
            detail = details.get(job_id, {})
            body = detail.get("requisitionDescription")
            job = Job(
                source=self.name,
                source_job_id=f"{cid}:{job_id}",
                title=item.get("requisitionTitle") or detail.get("requisitionTitle") or "",
                # `slug` is ADP's opaque customer id (cid), not the employer's real
                # name — the blocklist in cli.py's _fetch_boards can't match this
                # board to a blocked employer by name alone. Recovering the real
                # name would need it parsed out of the ADP API response here.
                company=slug,
                company_slug=slug,
                job_url=(
                    "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
                    f"recruitment.html?cid={cid}&jobId={job_id}&lang=en_US"
                ),
                location_raw=_location(item) or _location(detail),
                posted_at=item.get("postDate") or detail.get("postDate"),
                description_html=body,
                description_text=html_to_text(body) or None,
                raw=detail or item,
            )
            if job_id in known:
                job = shallow_refresh(job)
            jobs.append(job)
        return jobs
