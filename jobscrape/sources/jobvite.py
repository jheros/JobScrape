"""Jobvite career sites.

The listing is server-rendered HTML with no JSON endpoint, so job ids are read
from the permalinks and each posting's detail page supplies a schema.org
``JobPosting`` block — the same shape :mod:`jobscrape.sources.jsonld` already
reads for the other HTML boards.
"""

from __future__ import annotations

import re
from typing import Any

from ..http import Fetcher, gather_limited
from ..models import Job
from . import jsonld
from .base import BoardSource, Prefilter, shallow_refresh

LIST = "https://jobs.jobvite.com/{slug}/jobs"
DETAIL = "https://jobs.jobvite.com/{slug}/job/{job_id}"

#: Jobvite permalinks are `/job/<id>` where the id starts with 'o' and is
#: base62-ish. Anchored to the /job/ segment so nav links never match.
_JOB_ID_RE = re.compile(r"/job/([A-Za-z0-9]{6,})")


class Jobvite(BoardSource):
    name = "jobvite"
    ats = "jobvite"
    note = "Server-rendered listing; each posting's page carries JSON-LD."

    async def _detail(
        self, fetcher: Fetcher, slug: str, job_id: str
    ) -> tuple[str, dict[str, Any] | None]:
        html = await fetcher.get_text(DETAIL.format(slug=slug, job_id=job_id))
        return job_id, jsonld.first_job_posting(html)

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        known_ids: set[str] | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        html = await fetcher.get_text(LIST.format(slug=slug))
        if html is None:
            return None
        job_ids = list(dict.fromkeys(_JOB_ID_RE.findall(html)))
        if not job_ids:
            # The page answered but lists nothing — an empty board, not a dead
            # one. Only a failed fetch is a dead board.
            return []

        known = known_ids or set()
        details: dict[str, dict[str, Any]] = {}
        if with_details:
            # `known` holds `source_job_id` values straight from the DB
            # (`db.skippable_ids`), which for this adapter is `slug:jid` —
            # comparing it against the bare `jid` here meant this filter
            # could never actually match anything, so a detail request was
            # (re-)sent for literally every posting on every sweep,
            # defeating the whole point of `known_ids` (no request savings)
            # and never once exercising the `shallow_refresh` path below.
            needed = [j for j in job_ids if f"{slug}:{j}" not in known]
            fetched = await gather_limited(
                [self._detail(fetcher, slug, j) for j in needed], limit=4
            )
            for result in fetched:
                if isinstance(result, tuple) and isinstance(result[1], dict):
                    details[result[0]] = result[1]

        jobs: list[Job] = []
        for job_id in job_ids:
            posting = details.get(job_id)
            lo, hi, currency = jsonld.salary(posting)
            # `slug` is a reasonable first-insert fallback when there's
            # simply no hiringOrganization in the schema.org block — but an
            # already-known posting has no `posting` at all (`None`, never
            # fetched), and falling back to `slug` there sent it as if it
            # were a fresh update on every repeat sweep, overwriting the
            # real captured name. `""` instead lets `db.upsert_jobs`'s
            # NULLIF guard preserve whatever was already stored.
            company = jsonld.hiring_org(posting) or (slug if posting is not None else "")
            job = Job(
                source=self.name,
                source_job_id=f"{slug}:{job_id}",
                title=(posting or {}).get("title") or "",
                company=company,
                company_slug=slug,
                job_url=DETAIL.format(slug=slug, job_id=job_id),
                location_raw=jsonld.location_text(posting),
                is_remote=jsonld.is_remote(posting),
                employment_type=jsonld.employment_type(posting),
                industry=jsonld.industry(posting),
                posted_at=(posting or {}).get("datePosted"),
                salary_min=lo,
                salary_max=hi,
                salary_currency=currency,
                description_html=jsonld.description(posting),
                raw=posting or {},
            )
            if f"{slug}:{job_id}" in known:
                job = shallow_refresh(job)
            jobs.append(job)
        return jobs
