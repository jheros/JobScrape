"""Ashby job boards.

Two endpoints exist, and the difference is dramatic:

* **REST** (``api.ashbyhq.com/posting-api/job-board/{slug}``) returns *every*
  posting with its full description, publish date and compensation in **one
  request**. Measured on real boards: Notion 116 postings, OpenAI 748,
  bjakcareer 1,411 — each a single call.
* **GraphQL** (``jobs.ashbyhq.com/api/non-user-graphql``) needs one list call
  plus one detail call *per posting* for the same data — 117 requests for that
  Notion board instead of 1.

So REST is tried first and GraphQL is the fallback, which also disambiguates
something the HTTP layer can't: `Fetcher` returns ``None`` for a 404 and for a
transient network failure alike. If REST returns nothing we ask GraphQL before
concluding the board is dead, so a blip doesn't get recorded as a board
failure (which would feed `board_health`'s cooldown) and an unlisted-on-REST
board still gets collected.
"""

from __future__ import annotations

from typing import Any

from ..http import Fetcher, gather_limited
from ..models import Job
from .base import BoardSource, Prefilter, shallow_refresh

REST_ENDPOINT = "https://api.ashbyhq.com/posting-api/job-board"
ENDPOINT = "https://jobs.ashbyhq.com/api/non-user-graphql"

LIST_QUERY = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) {
    teams { id name parentTeamId }
    jobPostings {
      id title locationName employmentType teamId compensationTierSummary
    }
  }
}
""".strip()

# Field names verified empirically against the live endpoint — introspection is
# disabled, and any invalid field fails the entire query rather than being
# ignored, so do not add speculative fields here.
DETAIL_QUERY = """
query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName,
             jobPostingId: $jobPostingId) {
    id title descriptionHtml publishedDate employmentType locationName
    compensationTierSummary departmentName workplaceType applicationDeadline isListed
  }
}
""".strip()


class Ashby(BoardSource):
    name = "ashby"
    ats = "ashby"
    note = "Public REST board (one request per company); GraphQL fallback."

    # -- REST (preferred) ---------------------------------------------------

    async def _rest(self, fetcher: Fetcher, slug: str) -> list[dict[str, Any]] | None:
        data = await fetcher.get_json(
            f"{REST_ENDPOINT}/{slug}", params={"includeCompensation": "true"}
        )
        if not isinstance(data, dict):
            return None
        jobs = data.get("jobs")
        return jobs if isinstance(jobs, list) else None

    def _job_from_rest(self, slug: str, item: dict[str, Any]) -> Job:
        compensation = item.get("compensation") or {}
        summary = (
            compensation.get("compensationTierSummary")
            or compensation.get("scrapeableCompensationSalarySummary")
        )
        return Job(
            source=self.name,
            source_job_id=str(item.get("id")),
            title=item.get("title") or "",
            company=slug,
            company_slug=slug,
            job_url=item.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{item.get('id')}",
            apply_url=item.get("applyUrl"),
            location_raw=item.get("location"),
            is_remote=item.get("isRemote"),
            workplace_hint=item.get("workplaceType"),
            employment_type=item.get("employmentType"),
            department=item.get("department") or item.get("team"),
            posted_at=item.get("publishedAt"),
            description_html=item.get("descriptionHtml"),
            salary_raw=summary,
            raw=item,
        )

    # -- GraphQL (fallback) -------------------------------------------------

    async def _list(self, fetcher: Fetcher, slug: str) -> dict[str, Any] | None:
        data = await fetcher.post_json(
            f"{ENDPOINT}?op=ApiJobBoardWithTeams",
            {
                "operationName": "ApiJobBoardWithTeams",
                "variables": {"organizationHostedJobsPageName": slug},
                "query": LIST_QUERY,
            },
            headers={"content-type": "application/json"},
        )
        if not isinstance(data, dict) or data.get("errors"):
            return None
        return ((data.get("data") or {}).get("jobBoard")) or None

    async def _detail(
        self, fetcher: Fetcher, slug: str, posting_id: str
    ) -> dict[str, Any] | None:
        data = await fetcher.post_json(
            f"{ENDPOINT}?op=ApiJobPosting",
            {
                "operationName": "ApiJobPosting",
                "variables": {
                    "organizationHostedJobsPageName": slug,
                    "jobPostingId": posting_id,
                },
                "query": DETAIL_QUERY,
            },
            headers={"content-type": "application/json"},
        )
        if not isinstance(data, dict) or data.get("errors"):
            return None
        return (data.get("data") or {}).get("jobPosting")

    async def _fetch_graphql(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool,
        prefilter: Prefilter | None,
        known_ids: set[str] | None,
    ) -> list[Job] | None:
        board = await self._list(fetcher, slug)
        if board is None:
            return None
        postings = board.get("jobPostings") or []
        teams = {t["id"]: t.get("name") for t in (board.get("teams") or []) if t.get("id")}

        # The listing already carries title and location, so most non-matching
        # postings can be dropped before spending a detail request on them.
        candidates = [p for p in postings if p.get("id")]
        if prefilter is not None:
            candidates = [
                p for p in candidates
                if prefilter(
                    p.get("title"),
                    teams.get(p.get("teamId")),
                    p.get("locationName"),
                )
            ]

        # Postings already stored need no detail request — this is the single
        # biggest cost in a repeat sweep on this path.
        known = known_ids or set()
        needs_detail = [p for p in candidates if p["id"] not in known]

        details: dict[str, dict[str, Any]] = {}
        if with_details and needs_detail:
            # Kept low deliberately: this fans out *inside* an already-parallel
            # sweep of boards, and every Ashby board shares one hostname. The
            # host limiter is the real gate, but a smaller number here keeps the
            # queue from filling with requests for a single company.
            fetched = await gather_limited(
                [self._detail(fetcher, slug, p["id"]) for p in needs_detail],
                limit=3,
            )
            for result in fetched:
                if isinstance(result, dict) and result.get("id"):
                    details[result["id"]] = result

        jobs: list[Job] = []
        for item in candidates:
            detail = details.get(item["id"], {})
            workplace = str(detail.get("workplaceType") or "").lower()
            job = Job(
                source=self.name,
                source_job_id=item["id"],
                title=item.get("title") or detail.get("title") or "",
                company=slug,
                company_slug=slug,
                job_url=f"https://jobs.ashbyhq.com/{slug}/{item['id']}",
                apply_url=f"https://jobs.ashbyhq.com/{slug}/{item['id']}/application",
                location_raw=item.get("locationName") or detail.get("locationName"),
                is_remote="remote" in workplace if workplace else None,
                workplace_hint=detail.get("workplaceType"),
                employment_type=item.get("employmentType")
                or detail.get("employmentType"),
                department=detail.get("departmentName") or teams.get(item.get("teamId")),
                posted_at=detail.get("publishedDate"),
                description_html=detail.get("descriptionHtml"),
                salary_raw=item.get("compensationTierSummary")
                or detail.get("compensationTierSummary"),
                raw=detail or item if with_details else {},
            )
            # Already stored: keep the listing-derived fields, drop the rest so
            # the upsert preserves what we captured on the first pass.
            if item["id"] in known:
                job = shallow_refresh(job)
            jobs.append(job)
        return jobs

    # -- entry point --------------------------------------------------------

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        known_ids: set[str] | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        items = await self._rest(fetcher, slug)
        if items is not None:
            known = known_ids or set()
            jobs: list[Job] = []
            for item in items:
                if not item.get("id"):
                    continue
                job = self._job_from_rest(slug, item)
                # No prefilter shortcut is worth taking here: REST already
                # delivered every field in the one response that was going to
                # be made either way, so dropping a posting now saves nothing
                # and only risks discarding one the real filters would keep.
                if job.source_job_id in known:
                    job = shallow_refresh(job)
                jobs.append(job)
            return jobs

        return await self._fetch_graphql(fetcher, slug, with_details, prefilter, known_ids)
