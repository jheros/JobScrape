"""Adzuna aggregator API.

Free tier requires an app id + key (https://developer.adzuna.com/). Broad
long-tail coverage of smaller employers; descriptions are truncated snippets,
so treat it as breadth rather than depth.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .. import config
from ..http import Fetcher
from ..models import Job
from .base import DEFAULT_QUERIES, GlobalSource, JobBatch

API = "https://api.adzuna.com/v1/api/jobs/us/search/{page}"
PAGE_SIZE = 50
#: Tunable via JOBSCRAPE_ADZUNA_MAX_PAGES, and deliberately left at 20 — see
#: CALL_BUDGET, which is what actually binds once the term list grows.
MAX_PAGES = config.ADZUNA_MAX_PAGES

#: Total requests one fetch may spend, across every search term.
#:
#: Adzuna's free tier is quota-limited per day, and the old fixed 20-pages-per-
#: term spent ~220 calls against 11 terms. Adding terms under that rule would
#: have multiplied the spend straight past the quota — so the budget is fixed at
#: what it already costs and DIVIDED across whatever terms are configured.
#:
#: Which is the better buy anyway. Measured on the last sweep, Adzuna returned
#: 9,799 postings against an 11,000 ceiling, so the deep pages of each term are
#: nearly exhausted; page 1 of a term never searched returns more than page 20
#: of one already walked.
CALL_BUDGET = config.ADZUNA_CALL_BUDGET


class Adzuna(GlobalSource):
    name = "adzuna"
    requires_key = True
    note = "Broad SMB coverage. Needs free ADZUNA_APP_ID + ADZUNA_APP_KEY."

    def available(self) -> tuple[bool, str]:
        if not (config.ADZUNA_APP_ID and config.ADZUNA_APP_KEY):
            return False, "set ADZUNA_APP_ID and ADZUNA_APP_KEY (free: developer.adzuna.com)"
        return True, ""

    async def fetch(
        self,
        fetcher: Fetcher,
        since: date | None = None,
        until: date | None = None,
        queries: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[Job]:
        ok, _ = self.available()
        if not ok:
            return JobBatch()

        jobs: list[Job] = []
        seen: set[str] = set()
        # Cleared the moment this crawl stops short of its whole result set —
        # a bad page, or a cap. See JobBatch: only a crawl that walked
        # everything may be used to close postings it did not return.
        complete = True
        terms = queries or DEFAULT_QUERIES
        # Pages each term may walk, so the whole fetch stays inside CALL_BUDGET.
        # A shallower walk is not a complete crawl, and `complete` gates
        # source-wide closure — so it is cleared whenever the budget, rather
        # than the data, is what stopped us.
        per_term = max(1, min(MAX_PAGES, CALL_BUDGET // max(len(terms), 1)))
        if per_term < MAX_PAGES:
            complete = False
        for term in terms:
            exhausted = False
            for page in range(1, per_term + 1):
                params: dict[str, Any] = {
                    "app_id": config.ADZUNA_APP_ID,
                    "app_key": config.ADZUNA_APP_KEY,
                    "results_per_page": PAGE_SIZE,
                    "what_phrase": term,
                    "content-type": "application/json",
                    "sort_by": "date",
                }
                if since:
                    params["max_days_old"] = max((date.today() - since).days, 1)
                data = await fetcher.get_json(API.format(page=page), params=params)
                if not isinstance(data, dict):
                    complete = False  # the page failed; the rest is unread
                    break
                results = data.get("results") or []
                if not results:
                    exhausted = True
                    break
                for item in results:
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    job_id = str(item["id"])
                    if job_id in seen:
                        continue
                    seen.add(job_id)
                    location = item.get("location") or {}
                    area = location.get("area") or []
                    jobs.append(
                        Job(
                            source=self.name,
                            source_job_id=job_id,
                            title=item.get("title") or "",
                            company=(item.get("company") or {}).get("display_name") or "",
                            job_url=item.get("redirect_url") or "",
                            apply_url=item.get("redirect_url"),
                            location_raw=location.get("display_name"),
                            state=area[1] if len(area) > 1 else None,
                            city=area[-1] if len(area) > 2 else None,
                            country="US",
                            employment_type=item.get("contract_time"),
                            department=(item.get("category") or {}).get("label"),
                            posted_at=item.get("created"),
                            salary_min=item.get("salary_min"),
                            salary_max=item.get("salary_max"),
                            salary_currency="USD" if item.get("salary_min") else None,
                            description_text=item.get("description") or None,
                            raw=item,
                        )
                    )
                if len(results) < PAGE_SIZE:
                    exhausted = True
                    break
            else:
                # Ran the full page budget without a short page: this term had
                # more to give than the per-term allowance permitted.
                complete = False
            if not exhausted:
                complete = False
            if max_results and len(jobs) >= max_results:
                complete = False  # capped, not exhausted
                break
        return JobBatch(jobs, complete=complete)
