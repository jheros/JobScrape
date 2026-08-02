"""Oracle Recruiting Cloud (Fusion) career sites.

Large enterprises and the public sector. The listing endpoint carries titles,
locations and dates but not the description, so the body needs one detail call
per posting — the same shape as Ashby's GraphQL path.

A board is ``<host>/<siteNumber>``, e.g.
``ebwg.fa.us2.oraclecloud.com/CX``. The host is an opaque tenant code, so the
company name comes from the board registry rather than the payload.
"""

from __future__ import annotations

from typing import Any

from ..http import Fetcher, gather_limited
from ..models import Job, html_to_text
from .base import BoardSource, Prefilter, shallow_refresh

# `expand=requisitionList.secondaryLocations` is REQUIRED, not an optimisation:
# without it the endpoint returns 200 with an EMPTY requisitionList rather than
# an error, so the board reads as having no jobs instead of as misconfigured.
LIST = (
    "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    "?onlyData=true&expand=requisitionList.secondaryLocations"
    "&finder=findReqs;siteNumber={site},limit={limit},offset={offset}"
    ",sortBy=POSTING_DATES_DESC"
)
DETAIL = (
    "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
    "?expand=all&onlyData=true&finder=ById;Id=%22{job_id}%22,siteNumber={site}"
)
PAGE_SIZE = 100
#: Runaway guard. The largest tenants hold a few thousand postings.
MAX_PER_BOARD = 2000


def parse_slug(slug: str) -> tuple[str, str] | None:
    """Split ``host/siteNumber``; a bare host defaults to Oracle's usual ``CX``."""
    parts = slug.strip().split("/")
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    if len(parts) == 1 and parts[0]:
        return parts[0], "CX"
    return None


class Oracle(BoardSource):
    name = "oracle"
    ats = "oracle"
    note = "Oracle Recruiting Cloud REST; detail call needed for the description."

    async def _detail(
        self, fetcher: Fetcher, host: str, site: str, job_id: str
    ) -> dict[str, Any] | None:
        data = await fetcher.get_json(
            DETAIL.format(host=host, site=site, job_id=job_id),
            headers={"Accept": "application/json"},
        )
        if not isinstance(data, dict):
            return None
        items = data.get("items") or []
        return items[0] if items and isinstance(items[0], dict) else None

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
        host, site = parsed

        postings: list[dict[str, Any]] = []
        offset = 0
        total: int | None = None
        while offset < MAX_PER_BOARD:
            data = await fetcher.get_json(
                LIST.format(host=host, site=site, limit=PAGE_SIZE, offset=offset),
                headers={"Accept": "application/json"},
            )
            if not isinstance(data, dict):
                # A failure on the FIRST page means the board does not exist —
                # `None` is the "dead board" signal the validator and
                # board_health both read. A later failure just ends pagination
                # with what we already have, which is a real partial result and
                # must not be reported as a dead board.
                if offset == 0:
                    return None
                break
            items = data.get("items") or []
            batch = (items[0].get("requisitionList") or []) if items else []
            if total is None and items:
                total = int(items[0].get("TotalJobsCount") or 0)
            if not batch:
                break
            postings.extend(b for b in batch if isinstance(b, dict))
            offset += PAGE_SIZE
            if len(batch) < PAGE_SIZE or (total and offset >= total):
                break

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
                if prefilter(p.get("Title"), p.get("JobFamily"),
                             p.get("PrimaryLocation"))
            ]
            if not postings:
                return []

        known = known_ids or set()
        details: dict[str, dict[str, Any]] = {}
        if with_details:
            needed = [str(p.get("Id")) for p in postings
                      if p.get("Id") and str(p["Id"]) not in known]
            fetched = await gather_limited(
                [self._detail(fetcher, host, site, jid) for jid in needed], limit=4
            )
            for jid, result in zip(needed, fetched, strict=True):
                if isinstance(result, dict):
                    details[jid] = result

        return self._build(postings, host, site, details, slug, known)

    def _build(
        self,
        postings: list[dict[str, Any]],
        host: str,
        site: str,
        details: dict[str, dict[str, Any]],
        slug: str,
        known: set[str] | None,
    ) -> list[Job]:
        known = known or set()
        jobs: list[Job] = []
        for item in postings:
            job_id = str(item.get("Id") or "")
            if not job_id:
                continue
            detail = details.get(job_id, {})
            body = detail.get("ExternalDescriptionStr") or item.get(
                "ExternalQualificationsStr"
            )
            job = Job(
                source=self.name,
                source_job_id=f"{host}:{job_id}",
                title=item.get("Title") or detail.get("Title") or "",
                company=host.split(".")[0],
                company_slug=slug,
                job_url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}",
                apply_url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}/apply",
                location_raw=item.get("PrimaryLocation") or detail.get("PrimaryLocation"),
                # Oracle states the arrangement outright — "Hybrid" / "Remote" /
                # "On-site" — and puts it on the LIST response as well as the
                # detail, so it costs no extra request. Measured across two live
                # boards: 91 of 198 listings carry a value.
                #
                # The human-readable field, deliberately, not `WorkplaceTypeCode`:
                # `classify_workplace` substring-matches the hint, and while
                # ORA_HYBRID happens to contain "hybrid", a code such as
                # ORA_FULL_TIME_OFFICE would match the "office" branch by
                # accident. An empty string is falsy and simply falls through.
                workplace_hint=item.get("WorkplaceType") or detail.get("WorkplaceType"),
                employment_type=item.get("ContractType") or detail.get("ContractType"),
                department=item.get("JobFamily") or item.get("Department"),
                posted_at=item.get("PostedDate") or detail.get("PostedDate"),
                description_html=body,
                description_text=html_to_text(body) or None,
                raw=detail or item,
            )
            if job_id in known:
                job = shallow_refresh(job)
            jobs.append(job)
        return jobs
