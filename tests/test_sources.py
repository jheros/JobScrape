"""Adapter parsing tests against captured API response shapes.

The payload fixtures mirror what each provider actually returned when these
adapters were written, so a provider-side change or a parsing regression shows
up here rather than as quietly missing data.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobscrape.sources.greenhouse import Greenhouse
from jobscrape.sources.lever import Lever
from jobscrape.sources.workable import Workable


class FakeFetcher:
    """Returns queued payloads in order; records the URLs and bodies requested."""

    def __init__(self, *payloads: Any) -> None:
        self._payloads = list(payloads)
        self.urls: list[str] = []
        #: POST bodies, so a test can assert *what was asked for* and not just
        #: how many calls happened — e.g. that Workday enumerates with an empty
        #: searchText rather than guessing keywords.
        self.bodies: list[Any] = []

    async def get_json(self, url: str, **kw: Any) -> Any:
        self.urls.append(url)
        return self._payloads.pop(0) if self._payloads else None

    async def post_json(self, url: str, body: Any, **kw: Any) -> Any:
        self.urls.append(url)
        self.bodies.append(body)
        return self._payloads.pop(0) if self._payloads else None

    async def get_text(self, url: str, **kw: Any) -> Any:
        """The sitemap/HTML boards (iCIMS, Jobvite) read text, not JSON."""
        self.urls.append(url)
        return self._payloads.pop(0) if self._payloads else None


@pytest.mark.asyncio
async def test_greenhouse_parsing() -> None:
    payload = {
        "jobs": [
            {
                "id": 8044460,
                "title": "AI Engineer",
                "company_name": "Stripe",
                "absolute_url": "https://stripe.com/jobs/search?gh_jid=8044460",
                "location": {"name": "Chicago"},
                "first_published": "2026-07-03T12:00:00-04:00",
                "updated_at": "2026-07-27T11:17:30-04:00",
                "departments": [{"name": "Engineering"}],
                "offices": [{"name": "Chicago"}],
                "content": "<p>Build AI. The salary range is $150,000 - $220,000.</p>",
                "metadata": [],
            }
        ]
    }
    jobs = await Greenhouse().fetch_board(FakeFetcher(payload, {"name": "Stripe"}), "stripe")
    assert jobs is not None and len(jobs) == 1
    job = jobs[0]
    assert job.title == "AI Engineer"
    assert job.company == "Stripe"
    assert job.department == "Engineering"
    assert job.posted_at.startswith("2026-07-03")
    assert job.salary_min == 150_000 and job.salary_max == 220_000
    assert "Build AI" in job.description_text


@pytest.mark.asyncio
async def test_greenhouse_missing_board_returns_none() -> None:
    """A nonexistent board must be distinguishable from one with no openings."""
    assert await Greenhouse().fetch_board(FakeFetcher(None), "nope") is None
    empty = await Greenhouse().fetch_board(FakeFetcher({"jobs": []}, {"name": "X"}), "x")
    assert empty == []


@pytest.mark.asyncio
async def test_lever_epoch_ms_and_assembled_description() -> None:
    payload = [
        {
            "id": "a0fa7da3",
            "text": "Android Engineer",
            "hostedUrl": "https://jobs.lever.co/spotify/a0fa7da3",
            "applyUrl": "https://jobs.lever.co/spotify/a0fa7da3/apply",
            "createdAt": 1784569799619,  # epoch MILLISECONDS
            "country": "US",
            "workplaceType": "hybrid",
            "categories": {
                "location": "New York, NY",
                "department": "Engineering",
                "commitment": "Permanent",
                "allLocations": ["New York, NY"],
            },
            "description": "<p>Intro paragraph.</p>",
            "descriptionPlain": "Intro paragraph.",
            "lists": [{"text": "Requirements", "content": "<li>Kotlin</li>"}],
            "additional": "<p>Closing note.</p>",
            "additionalPlain": "Closing note.",
        }
    ]
    jobs = await Lever().fetch_board(FakeFetcher(payload), "spotify")
    assert jobs is not None and len(jobs) == 1
    job = jobs[0]
    # Milliseconds must not be read as seconds (which would land in 1970).
    assert job.posted_at.startswith("2026-")
    assert job.is_remote is False  # hybrid, not remote
    assert job.country == "US"
    # All three description parts must be present, not just the intro.
    assert "Intro paragraph" in job.description_text
    assert "Requirements" in job.description_text
    assert "Kotlin" in job.description_text
    assert "Closing note" in job.description_text


@pytest.mark.asyncio
async def test_workable_merges_one_requisition_across_locations() -> None:
    """Workable repeats a requisition per city; those must merge, not overwrite.

    Before this was handled, five of six copies were silently lost to the
    upsert and the extra cities disappeared with them.
    """
    payload = {
        "name": "Ace IT",
        "jobs": [
            {
                "shortcode": "3573F4975D", "title": "Junior QA Engineer",
                "city": "New York", "state": "New York", "country": "United States",
                "country_code": "US", "url": "https://apply.workable.com/ace/j/3573F4975D/",
                "published_on": "2026-07-01", "description": "<p>Test things.</p>",
            },
            {
                "shortcode": "3573F4975D", "title": "Junior QA Engineer",
                "city": "Newark", "state": "New Jersey", "country": "United States",
                "country_code": "US", "url": "https://apply.workable.com/ace/j/3573F4975D/",
                "published_on": "2026-07-01", "description": "<p>Test things.</p>",
            },
            {
                "shortcode": "3573F4975D", "title": "Junior QA Engineer",
                "city": "Philadelphia", "state": "Pennsylvania", "country": "United States",
                "country_code": "US", "url": "https://apply.workable.com/ace/j/3573F4975D/",
                "published_on": "2026-07-01", "description": "<p>Test things.</p>",
            },
        ],
    }
    jobs = await Workable().fetch_board(FakeFetcher(payload), "ace-it")
    assert jobs is not None
    assert len(jobs) == 1, "three copies of one requisition must collapse to one row"
    job = jobs[0]
    # Every city is retained.
    for city in ("New York", "Newark", "Philadelphia"):
        assert city in job.location_raw
    # The id is account-scoped, since shortcodes are only unique within a board.
    assert job.source_job_id == "ace-it:3573F4975D"


@pytest.mark.asyncio
async def test_workable_distinct_jobs_stay_distinct() -> None:
    payload = {
        "name": "Acme",
        "jobs": [
            {"shortcode": "AAA", "title": "Backend Engineer", "city": "Austin",
             "state": "TX", "country_code": "US"},
            {"shortcode": "BBB", "title": "Frontend Engineer", "city": "Austin",
             "state": "TX", "country_code": "US"},
        ],
    }
    jobs = await Workable().fetch_board(FakeFetcher(payload), "acme")
    assert jobs is not None and len(jobs) == 2
    assert {j.source_job_id for j in jobs} == {"acme:AAA", "acme:BBB"}


@pytest.mark.asyncio
async def test_smartrecruiters_uses_human_facing_url() -> None:
    """`job_url` must be a page a person can open, not an API endpoint.

    The list response's `ref` field looks like a URL but points at
    api.smartrecruiters.com; only the detail response carries the public link.
    """
    from jobscrape.sources.smartrecruiters import SmartRecruiters

    listing = {
        "totalFound": 1,
        "content": [
            {
                "id": "744000140390109",
                "name": "Software Engineer",
                "company": {"identifier": "AECOM2", "name": "AECOM"},
                "releasedDate": "2026-06-24T10:00:11.853Z",
                "location": {
                    "city": "Austin", "region": "TX", "country": "us",
                    "remote": False, "fullLocation": "Austin, TX, United States",
                },
                # Deliberately present, and deliberately not usable.
                "ref": "https://api.smartrecruiters.com/v1/companies/aecom2/postings/744000140390109",
            }
        ],
    }
    detail = {
        "id": "744000140390109",
        "postingUrl": "https://jobs.smartrecruiters.com/AECOM2/744000140390109-software-engineer",
        "applyUrl": "https://jobs.smartrecruiters.com/AECOM2/744000140390109-software-engineer?oga=true",
        "jobAd": {"sections": {"jobDescription": {"title": "Job", "text": "<p>Write code.</p>"}}},
    }
    jobs = await SmartRecruiters().fetch_board(FakeFetcher(listing, detail), "aecom2")
    assert jobs is not None and len(jobs) == 1
    job = jobs[0]
    assert "api.smartrecruiters.com" not in job.job_url
    assert job.job_url.startswith("https://jobs.smartrecruiters.com/")
    assert job.state == "TX" and job.country == "US"
    assert "Write code" in job.description_text


@pytest.mark.asyncio
async def test_smartrecruiters_url_falls_back_without_detail() -> None:
    """With no detail response we still construct a public URL, not an API one."""
    from jobscrape.sources.smartrecruiters import SmartRecruiters

    listing = {
        "totalFound": 1,
        "content": [{
            "id": "999",
            "name": "Backend Engineer",
            "company": {"identifier": "Acme", "name": "Acme"},
            "ref": "https://api.smartrecruiters.com/v1/companies/acme/postings/999",
        }],
    }
    jobs = await SmartRecruiters().fetch_board(FakeFetcher(listing, None), "acme")
    assert jobs is not None
    assert jobs[0].job_url == "https://jobs.smartrecruiters.com/Acme/999"


def test_hackernews_prefers_job_links_over_blog_posts() -> None:
    from jobscrape.sources.hackernews import _best_job_url

    html = ("Read our engineering blog at https://engineering.ramp.com/post/x "
            "and apply at https://jobs.ashbyhq.com/ramp/abc123")
    assert _best_job_url(html) == "https://jobs.ashbyhq.com/ramp/abc123"


def test_hackernews_returns_none_when_no_job_link() -> None:
    from jobscrape.sources.hackernews import _best_job_url

    assert _best_job_url("Just a blog: https://blog.example.com/post") is None
    assert _best_job_url("no links here") is None


# --- Ashby: REST primary, GraphQL fallback --------------------------------
#
# The REST endpoint returns every posting with its description, publish date
# and compensation in ONE request; GraphQL needs one list call plus one detail
# call per posting for the same data. Measured on a real 116-posting board:
# 1 request / 1.9s vs 117 requests / 82.4s.

#: Trimmed from a real api.ashbyhq.com/posting-api/job-board response.
_ASHBY_REST = {
    "apiVersion": "1",
    "jobs": [{
        "id": "05e14247-17c4-4e98-9a13-53828a4e2f13",
        "title": "Senior Backend Engineer",
        "department": "Engineering",
        "team": "Platform",
        "employmentType": "FullTime",
        "location": "New York, New York",
        "secondaryLocations": [],
        "publishedAt": "2026-04-02T21:00:55.755+00:00",
        "isListed": True,
        "isRemote": True,
        "workplaceType": "Hybrid",
        "jobUrl": "https://jobs.ashbyhq.com/Acme/05e14247-17c4-4e98-9a13-53828a4e2f13",
        "applyUrl": "https://jobs.ashbyhq.com/Acme/05e14247-17c4-4e98-9a13-53828a4e2f13/application",
        "descriptionHtml": "<p>Build the platform.</p>",
        "compensation": {
            "compensationTierSummary": "$211.4K – $290.6K • Offers Equity",
            "scrapeableCompensationSalarySummary": None,
            "compensationTiers": [],
            "summaryComponents": [],
        },
    }],
}


@pytest.mark.asyncio
async def test_ashby_rest_returns_full_detail_in_one_request() -> None:
    from jobscrape.sources.ashby import Ashby

    fetcher = FakeFetcher(_ASHBY_REST)
    jobs = await Ashby().fetch_board(fetcher, "Acme")

    assert jobs is not None and len(jobs) == 1
    assert len(fetcher.urls) == 1, "REST must not need a per-posting detail call"
    assert "api.ashbyhq.com" in fetcher.urls[0]

    job = jobs[0]
    assert job.title == "Senior Backend Engineer"
    assert job.description_text  # description arrives with the listing
    assert job.posted_at.startswith("2026-04-02")
    assert job.workplace_type == "hybrid"
    assert job.department == "Engineering"
    assert job.salary_min == 211_400 and job.salary_max == 290_600
    assert job.apply_url.endswith("/application")


@pytest.mark.asyncio
async def test_ashby_falls_back_to_graphql_when_rest_returns_nothing() -> None:
    """`Fetcher` returns None for a 404 and a transient failure alike, so REST
    coming back empty must not on its own be treated as "board is dead"."""
    from jobscrape.sources.ashby import Ashby

    graphql_list = {"data": {"jobBoard": {
        "teams": [{"id": "t1", "name": "Engineering"}],
        "jobPostings": [{
            "id": "abc", "title": "Backend Engineer", "locationName": "Remote",
            "employmentType": "FullTime", "teamId": "t1",
            "compensationTierSummary": None,
        }],
    }}}
    graphql_detail = {"data": {"jobPosting": {
        "id": "abc", "title": "Backend Engineer",
        "descriptionHtml": "<p>Build things.</p>",
        "publishedDate": "2026-07-01T00:00:00+00:00",
        "workplaceType": "Remote", "departmentName": "Engineering",
    }}}
    # First payload (REST) is None -> falls through to the GraphQL pair.
    fetcher = FakeFetcher(None, graphql_list, graphql_detail)
    jobs = await Ashby().fetch_board(fetcher, "Acme")

    assert jobs is not None and len(jobs) == 1
    assert jobs[0].description_text == "Build things."
    assert any("non-user-graphql" in u for u in fetcher.urls)


@pytest.mark.asyncio
async def test_ashby_dead_board_when_both_endpoints_fail() -> None:
    from jobscrape.sources.ashby import Ashby

    assert await Ashby().fetch_board(FakeFetcher(None, None), "nope") is None


@pytest.mark.asyncio
async def test_ashby_rest_shallow_refreshes_known_postings() -> None:
    """A posting already stored keeps its listing fields but drops the
    expensive ones, so the upsert's COALESCE preserves what we already have."""
    from jobscrape.sources.ashby import Ashby

    jobs = await Ashby().fetch_board(
        FakeFetcher(_ASHBY_REST), "Acme",
        known_ids={"05e14247-17c4-4e98-9a13-53828a4e2f13"},
    )
    assert jobs is not None and len(jobs) == 1
    assert jobs[0].description_html is None
    assert jobs[0].posted_at is None
    assert jobs[0].title == "Senior Backend Engineer"  # listing fields survive


# --- Workday: empty-search enumeration ------------------------------------
#
# Workday has no "list everything" call, but an EMPTY searchText enumerates the
# whole board. Measured against live tenants, keyword search silently
# under-collects by a wide margin: AIG 475 postings vs 28, 3M 655 vs 41,
# NVIDIA 2,000 vs 1,694 — and after filtering, AIG yielded 37 real software
# roles via empty search against 15 via keywords.


def _wd_page(n: int, total: int, start: int = 0) -> dict[str, Any]:
    return {
        "total": total,
        "jobPostings": [
            {
                "title": f"Software Engineer {start + i}",
                "externalPath": f"/job/req{start + i}",
                "locationsText": "Austin, TX",
                "bulletFields": [f"REQ{start + i}"],
            }
            for i in range(n)
        ],
    }


@pytest.mark.asyncio
async def test_workday_uses_empty_search_to_enumerate_the_board() -> None:
    from jobscrape.sources.workday import Workday

    fetcher = FakeFetcher(_wd_page(20, 20), *[None] * 30)
    jobs = await Workday().fetch_board(fetcher, "acme|wd1|Careers", with_details=False)

    assert jobs is not None and len(jobs) == 20
    # The very first call must be the listing with an empty searchText — i.e.
    # enumeration, not a keyword guess.
    assert fetcher.bodies[0]["searchText"] == ""


@pytest.mark.asyncio
async def test_workday_stops_at_total_rather_than_waiting_for_a_short_page() -> None:
    """Workday keeps serving full pages past the end of the result set
    (offset=2000 on a 2,000-posting board returns 20 more rows), so pagination
    that waited for a short page to signal the end would never terminate.

    Exercises `_search` directly: the full pagination loop only runs on the
    detail-fetching path, while `with_details=False` deliberately reads a
    single page because probing just needs to prove the board responds.
    """
    from jobscrape.sources.workday import Workday

    # Every page is full — `total` is the only thing that says when to stop.
    fetcher = FakeFetcher(*[_wd_page(20, 40, start=i * 20) for i in range(10)])
    found = await Workday()._search(fetcher, "https://x/cxs", "", limit=1000)

    assert len(found) == 40, "must stop at total=40 rather than paginating forever"
    assert len(fetcher.urls) == 2


@pytest.mark.asyncio
async def test_workday_probe_reads_only_one_page() -> None:
    """Validation just needs to know the board answers, not to enumerate it."""
    from jobscrape.sources.workday import Workday

    fetcher = FakeFetcher(*[_wd_page(20, 500, start=i * 20) for i in range(10)])
    status, count = await Workday().probe(fetcher, "acme|wd1|Careers")

    assert status == "live" and count == 20
    assert len(fetcher.urls) == 1


@pytest.mark.asyncio
async def test_workday_falls_back_to_keyword_search_when_empty_returns_nothing() -> None:
    from jobscrape.sources.workday import Workday

    # First call (empty search) yields nothing; the keyword fallback then works.
    fetcher = FakeFetcher({"total": 0, "jobPostings": []}, _wd_page(3, 3))
    jobs = await Workday().fetch_board(fetcher, "acme|wd1|Careers", with_details=False)

    assert jobs is not None and len(jobs) == 3
    assert fetcher.bodies[0]["searchText"] == ""
    assert fetcher.bodies[1]["searchText"], "fallback must send a real keyword"


# --- global-source crawl completeness -------------------------------------
#
# Source-wide closure is only sound against a crawl that walked its whole
# result set. Every global adapter stops early and returns what it has when a
# page fails or a cap is hit, so a rate-limited run is indistinguishable from a
# genuinely shrunken catalogue unless the adapter says which it was. Closing
# against a truncated set retires postings the run never reached.


def test_plain_list_is_read_as_incomplete() -> None:
    """The safe default: anything that does not explicitly claim completeness
    is treated as incomplete, so forgetting to set it under-closes rather than
    deleting live jobs."""
    from jobscrape.sources.base import JobBatch, is_complete

    assert is_complete([]) is False
    assert is_complete(["a", "b"]) is False
    assert is_complete(JobBatch()) is False
    assert is_complete(JobBatch([], complete=True)) is True


def test_job_batch_behaves_as_a_list() -> None:
    """A list subclass so every existing len()/iteration call site is unaffected."""
    from jobscrape.sources.base import JobBatch

    batch = JobBatch([1, 2, 3], complete=True)
    assert len(batch) == 3
    assert list(batch) == [1, 2, 3]
    assert batch[0] == 1


@pytest.mark.asyncio
async def test_adzuna_exhausted_crawl_is_complete() -> None:
    from jobscrape import config as cfg
    from jobscrape.sources.adzuna import Adzuna
    from jobscrape.sources.base import is_complete

    cfg.ADZUNA_APP_ID, cfg.ADZUNA_APP_KEY = "id", "key"
    short_page = {"results": [{"id": "1", "title": "Engineer",
                               "company": {"display_name": "Acme"},
                               "redirect_url": "https://x/1",
                               "location": {"display_name": "Austin"}}]}
    jobs = await Adzuna().fetch(FakeFetcher(short_page), queries=["engineer"])
    assert len(jobs) == 1
    assert is_complete(jobs) is True


@pytest.mark.asyncio
async def test_adzuna_failed_page_marks_the_crawl_incomplete() -> None:
    """THE regression test. A mid-pagination failure must not look like a
    complete crawl, or the source-wide close retires everything past the
    failure — and the 48h grace hides it until failures repeat, which is
    exactly when rate-limiting happens."""
    from jobscrape import config as cfg
    from jobscrape.sources.adzuna import Adzuna
    from jobscrape.sources.base import is_complete

    cfg.ADZUNA_APP_ID, cfg.ADZUNA_APP_KEY = "id", "key"
    full_page = {"results": [
        {"id": str(i), "title": "Engineer", "company": {"display_name": "Acme"},
         "redirect_url": f"https://x/{i}", "location": {"display_name": "Austin"}}
        for i in range(50)
    ]}
    # Page 1 is full, page 2 fails -> partial results, and the adapter must say so.
    jobs = await Adzuna().fetch(FakeFetcher(full_page, None), queries=["engineer"])
    assert len(jobs) == 50, "the postings it did read are still returned"
    assert is_complete(jobs) is False, "but the crawl must not claim completeness"


@pytest.mark.asyncio
async def test_capped_crawl_is_incomplete() -> None:
    from jobscrape import config as cfg
    from jobscrape.sources.adzuna import Adzuna
    from jobscrape.sources.base import is_complete

    cfg.ADZUNA_APP_ID, cfg.ADZUNA_APP_KEY = "id", "key"
    full_page = {"results": [
        {"id": str(i), "title": "Engineer", "company": {"display_name": "Acme"},
         "redirect_url": f"https://x/{i}", "location": {"display_name": "Austin"}}
        for i in range(50)
    ]}
    jobs = await Adzuna().fetch(FakeFetcher(full_page, full_page), queries=["e"], max_results=10)
    assert is_complete(jobs) is False


@pytest.mark.asyncio
async def test_sampling_sources_are_never_complete() -> None:
    """hackernews reads a bounded set of threads and jsearch has a hard request
    budget; neither ever enumerates a catalogue, so neither may ever be a basis
    for closing postings it did not return."""
    from jobscrape.sources.base import is_complete
    from jobscrape.sources.hackernews import HackerNews

    jobs = await HackerNews().fetch(FakeFetcher({"hits": []}))
    assert is_complete(jobs) is False


# --- new enterprise ATS adapters -------------------------------------------
#
# Every payload below is trimmed from a real live response captured while
# building the adapter, so a provider-side shape change shows up here.


@pytest.mark.asyncio
async def test_oracle_requires_the_expand_parameter() -> None:
    """Oracle returns 200 with an EMPTY requisitionList when `expand` is
    missing — the board reads as having no jobs rather than as misconfigured,
    which is exactly the kind of silent nothing that is hard to notice."""
    from jobscrape.sources.oracle import LIST

    assert "expand=requisitionList.secondaryLocations" in LIST


@pytest.mark.asyncio
async def test_oracle_parsing() -> None:
    from jobscrape.sources.oracle import Oracle

    listing = {"items": [{
        "TotalJobsCount": 1,
        "requisitionList": [{
            "Id": "19602", "Title": "Sr Software Engineer",
            "PrimaryLocation": "Norcross, GA, United States",
            "PostedDate": "2026-07-28", "JobFamily": "Engineering",
            "ContractType": "Full time",
        }],
    }]}
    detail = {"items": [{
        "Id": "19602", "ExternalDescriptionStr": "<p>Build payments systems.</p>",
    }]}
    jobs = await Oracle().fetch_board(
        FakeFetcher(listing, detail), "ebwg.fa.us2.oraclecloud.com/CX"
    )
    assert jobs is not None and len(jobs) == 1
    job = jobs[0]
    assert job.title == "Sr Software Engineer"
    assert job.source_job_id == "ebwg.fa.us2.oraclecloud.com:19602"
    assert "Build payments systems" in job.description_text
    assert job.posted_at.startswith("2026-07-28")


def test_oracle_slug_defaults_the_site_number() -> None:
    from jobscrape.sources.oracle import parse_slug

    assert parse_slug("host.example.com/CX_2") == ("host.example.com", "CX_2")
    assert parse_slug("host.example.com") == ("host.example.com", "CX")
    assert parse_slug("") is None


@pytest.mark.asyncio
async def test_ukg_needs_no_detail_call() -> None:
    """UKG's search response already carries the description, so a board costs
    one request per page rather than one per posting."""
    from jobscrape.sources.ukg import UKG

    page = {"totalCount": 1, "opportunities": [{
        "Id": "176b2423", "Title": "Housing Certification Specialist",
        "RequisitionNumber": "HOUSI001282", "FullTime": True,
        "JobCategoryName": "Administrative", "PostedDate": "2026-07-30T16:52:32Z",
        "BriefDescription": "We are seeking a specialist.",
        "Locations": [{"Address": {"City": {"Name": "Saint Paul"},
                                   "State": {"Code": "MN"},
                                   "Country": {"Code": "USA"}}}],
    }]}
    fetcher = FakeFetcher(page)
    jobs = await UKG().fetch_board(
        fetcher, "acme.rec.pro.ukg.net/tenant/guid-here"
    )
    assert jobs is not None and len(jobs) == 1
    assert len(fetcher.urls) == 1, "no per-posting detail request"
    job = jobs[0]
    assert job.title == "Housing Certification Specialist"
    assert job.location_raw == "Saint Paul, MN, USA"
    assert job.description_text == "We are seeking a specialist."


def test_ukg_rejects_a_malformed_slug() -> None:
    from jobscrape.sources.ukg import parse_slug

    assert parse_slug("host/tenant/guid") == ("host", "tenant", "guid")
    assert parse_slug("host/tenant") is None
    assert parse_slug("nope") is None


def test_icims_recognises_all_three_posting_url_shapes() -> None:
    """iCIMS serves postings under three different paths depending on product
    and whether the tenant uses a vanity host."""
    from jobscrape.sources.icims import _job_id

    assert _job_id("https://careers-360care.icims.com/jobs/5059/helpdesk-tech/job") == "5059"
    assert _job_id("https://x.icims.com/careers-home/jobs/12345") == "12345"
    assert _job_id("https://careers.docusign.com/jobs/30010?lang=en-us") == "30010"
    # The board's own search page is in every sitemap and must never be a job.
    assert _job_id("https://careers-360care.icims.com/jobs/search") is None


def test_icims_board_host_handles_slug_and_vanity_forms() -> None:
    from jobscrape.sources.icims import board_host

    assert board_host("360care") == "careers-360care.icims.com"
    assert board_host("careers.docusign.com") == "careers.docusign.com"
    # Idempotent: certificate transparency yields the whole label while Common
    # Crawl's regex yields the bare tenant, and both must land on one host.
    assert board_host("careers-360care") == "careers-360care.icims.com"


def test_icims_board_host_keeps_non_careers_prefixes() -> None:
    """iCIMS serves boards on several prefixes, and only `careers-` was known.

    Certificate transparency returns whole labels, so `us-shermco` was read as
    a tenant named "us-shermco" and resolved to `careers-us-shermco.icims.com`
    — which 404s. Six registered boards are wrong this way; the prefix list is
    taken from the 715 slugs already stored in dotted form, so it keeps up as
    CT finds more of them.
    """
    from jobscrape.sources.icims import board_host

    assert board_host("us-shermco") == "us-shermco.icims.com"
    assert board_host("usa-titanmaterials") == "usa-titanmaterials.icims.com"
    assert board_host("external-92y") == "external-92y.icims.com"
    assert board_host("uscareers-asm") == "uscareers-asm.icims.com"
    # A hyphen alone means nothing — a real tenant name may contain one.
    assert board_host("1stheritage-cu") == "careers-1stheritage-cu.icims.com"


def test_rippling_joins_its_sectioned_description() -> None:
    """Rippling serves `description` as an OBJECT of named sections, not a
    string — handing it straight to the HTML flattener raises TypeError."""
    from jobscrape.sources.rippling import _description

    assert _description({"description": {"company": "<p>About us</p>",
                                         "role": "<p>The role</p>"}}) == \
        "<p>About us</p>\n<p>The role</p>"
    assert _description({"description": "<p>Plain</p>"}) == "<p>Plain</p>"
    assert _description({}) is None


def test_rippling_tolerates_an_empty_pay_band() -> None:
    """An unset band comes back as an empty LIST, not an empty object."""
    from jobscrape.sources.rippling import _pay

    assert _pay({"payRangeDetails": []}) == (None, None, None)
    assert _pay({"payRangeDetails": {"min": 1, "max": 2, "currency": "USD"}}) == \
        (1.0, 2.0, "USD")


@pytest.mark.asyncio
async def test_adp_parsing_and_nested_location() -> None:
    from jobscrape.sources.adp import ADP

    listing = {"jobRequisitions": [{
        "itemID": "9201341333045_1",
        "requisitionTitle": "Technical Account Manager",
        "postDate": "2026-07-30T11:57:00.000-04:00",
        "requisitionLocations": [{"address": {
            "cityName": "Austin",
            "countrySubdivisionLevel1": {"codeValue": "TX"},
            "countryCode": {"codeValue": "US"},
        }}],
    }]}
    detail = {"jobRequisitions": [{
        "itemID": "9201341333045_1",
        "requisitionDescription": "<p>Help customers succeed.</p>",
    }]}
    jobs = await ADP().fetch_board(FakeFetcher(listing, detail), "cid-guid")
    assert jobs is not None and len(jobs) == 1
    job = jobs[0]
    assert job.title == "Technical Account Manager"
    assert job.location_raw == "Austin, TX, US"
    assert "Help customers succeed" in job.description_text


def test_adp_slug_accepts_an_optional_cc_id() -> None:
    from jobscrape.sources.adp import parse_slug

    assert parse_slug("cid-guid") == ("cid-guid", None)
    assert parse_slug("cid-guid:9201289910657_2") == ("cid-guid", "9201289910657_2")


@pytest.mark.asyncio
async def test_new_board_adapters_report_a_dead_board_as_none() -> None:
    """`None` means the board does not exist and `[]` means it exists but is
    empty — the validator and board_health read the difference."""
    from jobscrape.sources.adp import ADP
    from jobscrape.sources.icims import ICIMS
    from jobscrape.sources.oracle import Oracle
    from jobscrape.sources.rippling import Rippling
    from jobscrape.sources.ukg import UKG

    assert await Oracle().fetch_board(FakeFetcher(None), "h/CX") is None
    assert await UKG().fetch_board(FakeFetcher(None), "h/t/g") is None
    assert await ADP().fetch_board(FakeFetcher(None), "cid") is None
    assert await Rippling().fetch_board(FakeFetcher(None), "slug") is None
    assert await ICIMS().fetch_board(FakeFetcher(None), "slug") is None


# --- Workday 2,000 reporting cap -------------------------------------------
#
# Workday answers `total: 2000` for any board with at least that many postings,
# and paginating past it wraps to page 1. Measured on NVIDIA: `total` reads
# 2000 while the jobFamilyGroup facet counts sum to 2,583 — so a board sitting
# on the cap is silently truncated unless the query is narrowed by facet.


def _wd_capped_page(facet_values: int = 3) -> dict[str, Any]:
    """A first page that reports the cap and offers a facet to split on."""
    return {
        "total": 2000,
        "jobPostings": [
            {"title": f"Engineer {i}", "externalPath": f"/job/capped{i}",
             "bulletFields": [f"CAP{i}"]}
            for i in range(20)
        ],
        "facets": [{
            "facetParameter": "jobFamilyGroup",
            "values": [{"id": f"fid{i}", "descriptor": f"Family {i}", "count": 500}
                       for i in range(facet_values)],
        }],
    }


def _wd_branch_page(prefix: str, n: int, total: int) -> dict[str, Any]:
    return {
        "total": total,
        "jobPostings": [
            {"title": f"{prefix} {i}", "externalPath": f"/job/{prefix}{i}",
             "bulletFields": [f"{prefix}{i}"]}
            for i in range(n)
        ],
        "facets": [],
    }


@pytest.mark.asyncio
async def test_workday_subdivides_when_the_total_is_capped() -> None:
    """A board reporting exactly 2000 must be narrowed by facet, not accepted."""
    from jobscrape.sources.workday import Workday

    fetcher = FakeFetcher(
        _wd_capped_page(facet_values=2),   # probe: reports the cap
        _wd_branch_page("a", 5, 5),        # branch 1 first page (under cap)
        _wd_branch_page("a", 5, 5),        # branch 1 paged read
        _wd_branch_page("b", 5, 5),        # branch 2 first page
        _wd_branch_page("b", 5, 5),        # branch 2 paged read
    )
    found = await Workday()._enumerate(
        fetcher, "https://x/cxs", limit=2000
    )
    paths = {p["externalPath"] for p in found}
    assert paths == {f"/job/a{i}" for i in range(5)} | {f"/job/b{i}" for i in range(5)}
    # The capped page's own rows are NOT used — the branches replace them.
    assert not any(p.startswith("/job/capped") for p in paths)
    # It applied a facet rather than paging the capped query.
    assert any(b.get("appliedFacets") for b in fetcher.bodies)


@pytest.mark.asyncio
async def test_workday_does_not_subdivide_below_the_cap() -> None:
    """A real count is pageable, so narrowing would just cost requests."""
    from jobscrape.sources.workday import Workday

    fetcher = FakeFetcher(
        _wd_branch_page("x", 5, 5),   # probe: real total
        _wd_branch_page("x", 5, 5),   # the paged read
    )
    found = await Workday()._enumerate(fetcher, "https://x/cxs", limit=2000)
    assert len(found) == 5
    assert all(not b.get("appliedFacets") for b in fetcher.bodies)


@pytest.mark.asyncio
async def test_workday_dedupes_postings_across_facet_branches() -> None:
    """One posting can sit under several facet values, so branches overlap."""
    from jobscrape.sources.workday import Workday

    overlap = _wd_branch_page("same", 3, 3)
    fetcher = FakeFetcher(
        _wd_capped_page(facet_values=2),
        overlap, overlap,   # branch 1
        overlap, overlap,   # branch 2 returns the SAME postings
    )
    jobs = await Workday().fetch_board(fetcher, "acme|wd1|Careers")
    assert jobs is not None
    assert len({j.source_job_id for j in jobs}) == len(jobs), "no duplicate ids"
    assert len(jobs) == 3


@pytest.mark.asyncio
async def test_workday_takes_the_capped_page_when_no_facet_can_narrow() -> None:
    """Under-collecting beats collecting nothing."""
    from jobscrape.sources.workday import Workday

    capped_no_facets = dict(_wd_capped_page(), facets=[])
    fetcher = FakeFetcher(capped_no_facets, capped_no_facets)
    found = await Workday()._enumerate(fetcher, "https://x/cxs", limit=2000)
    assert len(found) == 20
