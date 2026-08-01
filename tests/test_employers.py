"""Direct employer career sites: Amazon, Apple, Uber.

Each of these has one failure mode that answers HTTP 200 and looks like data,
so the tests below are mostly about telling working from merely-plausible:

* Uber paginates only when ``limit``/``page`` are top-level. Nested inside
  ``params`` it returns the identical result set for every page, forever.
* Apple ignores a wrong location filter key rather than rejecting it, returning
  the full worldwide catalogue with a 200.
* Amazon stops serving rows past 10,000 for one query without saying so.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobscrape.sources.base import is_complete
from jobscrape.sources.employers import Amazon, Apple, Uber


class FakeFetcher:
    def __init__(self, gets: list[Any] | None = None, posts: list[Any] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        self._gets = list(gets or [])
        self._posts = list(posts or [])
        self._headers = headers
        self.params: list[dict[str, Any]] = []
        self.bodies: list[Any] = []
        self.urls: list[str] = []

    async def get_json(self, url: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        self.urls.append(url)
        self.params.append(params or {})
        return self._gets.pop(0) if self._gets else None

    async def post_json(self, url: str, body: Any, **kw: Any) -> Any:
        self.urls.append(url)
        self.bodies.append(body)
        return self._posts.pop(0) if self._posts else None

    async def get_headers(self, url: str, **kw: Any) -> Any:
        self.urls.append(url)
        return self._headers


# =========================================================================
# Amazon
# =========================================================================

def _amz(n: int, hits: int, start: int = 0) -> dict[str, Any]:
    return {
        "hits": hits,
        "jobs": [
            {
                "id_icims": f"{1000 + i}",
                "title": f"Software Development Engineer {i}",
                "job_path": f"/en/jobs/{1000 + i}/sde-{i}",
                "normalized_location": "Seattle, Washington, USA",
                "city": "Seattle", "state": "Washington", "country_code": "USA",
                "business_category": "aws",
                "job_schedule_type": "Full-Time",
                "posted_date": "July 30, 2026",
                "description": "Build distributed systems.",
                "basic_qualifications": "- 3+ years of experience",
                "preferred_qualifications": "- Experience with Rust",
            }
            for i in range(start, start + n)
        ],
    }


@pytest.mark.asyncio
async def test_amazon_joins_the_qualification_fields_into_the_description() -> None:
    """Requirements live outside `description`; dropping them loses the part
    role filtering depends on most."""
    jobs = await Amazon().fetch(FakeFetcher(gets=[_amz(1, 1)]))
    assert len(jobs) == 1
    job = jobs[0]
    assert "Build distributed systems" in job.description_text
    assert "3+ years of experience" in job.description_text
    assert "Experience with Rust" in job.description_text
    assert job.company == "Amazon"
    assert job.job_url == "https://www.amazon.jobs/en/jobs/1000/sde-0"
    assert job.city == "Seattle" and job.country == "US"   # normalized from "USA"
    # "July 30, 2026" is display text, not a timestamp.
    assert job.posted_at.startswith("2026-07-30")


@pytest.mark.asyncio
async def test_amazon_paginates_to_the_hit_count() -> None:
    fetcher = FakeFetcher(gets=[_amz(100, 150, 0), _amz(50, 150, 100)])
    jobs = await Amazon().fetch(fetcher)
    assert len(jobs) == 150
    assert is_complete(jobs)
    assert fetcher.params[0]["offset"] == 0 and fetcher.params[1]["offset"] == 100


@pytest.mark.asyncio
async def test_amazon_marks_a_capped_query_incomplete() -> None:
    """Past 10,000 Amazon serves nothing more; closing on that would retire
    every posting the run could not reach."""
    fetcher = FakeFetcher(gets=[_amz(100, 25_000, i * 100) for i in range(100)])
    jobs = await Amazon().fetch(fetcher)
    assert not is_complete(jobs), "a truncated crawl must never drive closure"


@pytest.mark.asyncio
async def test_amazon_scopes_the_search_to_the_us() -> None:
    fetcher = FakeFetcher(gets=[_amz(1, 1)])
    await Amazon().fetch(fetcher)
    assert fetcher.params[0]["country"] == "USA"


@pytest.mark.asyncio
async def test_amazon_a_failed_page_is_not_a_complete_crawl() -> None:
    jobs = await Amazon().fetch(FakeFetcher(gets=[None]))
    assert jobs == [] and not is_complete(jobs)


# =========================================================================
# Apple
# =========================================================================

def _apple_row(position: str, store: str, city: str) -> dict[str, Any]:
    return {
        "id": f"{position}-{store}",
        "positionId": position,
        "postingTitle": "Software Engineer",
        "jobSummary": "Work on the platform.",
        "postDateInGMT": "2026-07-30T19:11:10Z",
        "postingDate": "Jul 30, 2026",
        "locations": [{"name": city, "countryName": "United States of America"}],
        "team": {"teamName": "Software and Services"},
    }


def _apple_page(rows: list[dict[str, Any]], total: int) -> dict[str, Any]:
    return {"res": {"searchResults": rows, "totalRecords": total}}


@pytest.mark.asyncio
async def test_apple_uses_the_location_filter_key_that_actually_filters() -> None:
    """`postingpostLocation` and `postLocation` return 200 and the full
    worldwide set — a wrong key looks like a working one."""
    fetcher = FakeFetcher(headers={"X-Apple-CSRF-Token": "tok"},
                          posts=[_apple_page([_apple_row("1", "0001", "Cupertino")], 1)])
    await Apple().fetch(fetcher)
    assert fetcher.bodies[0]["filters"] == {"locations": ["postLocation-USA"]}


@pytest.mark.asyncio
async def test_apple_reads_results_from_under_res() -> None:
    """Top-level `searchResults` is absent; looking there reports an empty board."""
    fetcher = FakeFetcher(headers={"X-Apple-CSRF-Token": "tok"},
                          posts=[_apple_page([_apple_row("200", "0001", "Cupertino")], 1)])
    jobs = await Apple().fetch(fetcher)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source_job_id == "200"
    assert job.company == "Apple"
    assert job.department == "Software and Services"
    assert job.job_url.endswith("/details/200")
    assert job.posted_at.startswith("2026-07-30")


@pytest.mark.asyncio
async def test_apple_merges_one_requisition_across_stores() -> None:
    """`id` is `{positionId}-{storeCode}`; keeping the first would drop the
    other locations, as it would on Workable."""
    fetcher = FakeFetcher(
        headers={"X-Apple-CSRF-Token": "tok"},
        posts=[_apple_page([
            _apple_row("500", "3760", "Santa Clara"),
            _apple_row("500", "3337", "Seattle"),
            _apple_row("501", "0001", "Cupertino"),
        ], 3)],
    )
    jobs = await Apple().fetch(fetcher)
    assert len(jobs) == 2
    merged = next(j for j in jobs if j.source_job_id == "500")
    assert "Santa Clara" in merged.location_raw
    assert "Seattle" in merged.location_raw


@pytest.mark.asyncio
async def test_apple_elides_a_very_long_location_list_without_hiding_it() -> None:
    rows = [_apple_row("700", f"{i:04d}", f"City{i}") for i in range(12)]
    fetcher = FakeFetcher(headers={"X-Apple-CSRF-Token": "tok"},
                          posts=[_apple_page(rows, 12)])
    jobs = await Apple().fetch(fetcher)
    assert len(jobs) == 1
    # Truncated, but it says so — a cut list must never read as a complete one.
    assert jobs[0].location_raw.endswith("+4 more")
    assert jobs[0].location_raw.count(";") == 8


@pytest.mark.asyncio
async def test_apple_without_a_csrf_token_returns_nothing_and_calls_nothing() -> None:
    fetcher = FakeFetcher(headers={})
    jobs = await Apple().fetch(fetcher)
    assert jobs == []
    assert fetcher.bodies == [], "the search must not be attempted without a token"


@pytest.mark.asyncio
async def test_apple_paginates_by_total_records() -> None:
    page1 = _apple_page([_apple_row(str(i), "0001", "Cupertino") for i in range(20)], 25)
    page2 = _apple_page([_apple_row(str(20 + i), "0001", "Cupertino") for i in range(5)], 25)
    fetcher = FakeFetcher(headers={"X-Apple-CSRF-Token": "tok"}, posts=[page1, page2])
    jobs = await Apple().fetch(fetcher)
    assert len(jobs) == 25
    assert [b["page"] for b in fetcher.bodies] == [1, 2]


# =========================================================================
# Uber
# =========================================================================

def _uber_page(ids: list[int], total: int) -> dict[str, Any]:
    return {
        "data": {
            # A Long object, not a number — int() on it raises.
            "totalResults": {"low": total, "high": 0, "unsigned": False},
            "results": [
                {
                    "id": i,
                    "title": f"Software Engineer {i}",
                    "department": "Engineer - Risk &amp; Safety",
                    "location": {"country": "USA", "region": "Santa Clara",
                                 "city": "Sunnyvale", "countryName": "United States"},
                    "allLocations": [{"country": "USA", "region": "Santa Clara",
                                      "city": "Sunnyvale", "countryName": "United States"}],
                    "creationDate": "2026-06-19T15:12:02.000Z",
                    "updatedDate": "2026-07-27T14:44:26.000Z",
                    "description": "",
                }
                for i in ids
            ],
        }
    }


@pytest.mark.asyncio
async def test_uber_sends_limit_and_page_at_the_top_level() -> None:
    """Nested in `params`, every page returns the identical result set — 200 and
    the right total, so it looks correct while collecting one page forever."""
    fetcher = FakeFetcher(posts=[_uber_page([1], 1)])
    await Uber().fetch(fetcher)
    body = fetcher.bodies[0]
    assert "limit" in body and "page" in body
    assert "limit" not in body["params"] and "page" not in body["params"]


@pytest.mark.asyncio
async def test_uber_decodes_the_long_shaped_total() -> None:
    """`totalResults` is {"low": n, ...}; a naive int() raises and a 0 default
    would end pagination after one page."""
    fetcher = FakeFetcher(posts=[
        _uber_page(list(range(100)), 150),
        _uber_page(list(range(100, 150)), 150),
    ])
    jobs = await Uber().fetch(fetcher)
    assert len(jobs) == 150
    assert [b["page"] for b in fetcher.bodies] == [0, 1]


@pytest.mark.asyncio
async def test_uber_stops_when_a_page_is_entirely_duplicates() -> None:
    """The backstop against the nested-params trap becoming an endless loop."""
    same = list(range(100))
    fetcher = FakeFetcher(posts=[_uber_page(same, 500) for _ in range(10)])
    jobs = await Uber().fetch(fetcher)
    assert len(jobs) == 100
    assert len(fetcher.bodies) == 2, "one repeat is enough to detect it"


@pytest.mark.asyncio
async def test_uber_parses_and_unescapes() -> None:
    jobs = await Uber().fetch(FakeFetcher(posts=[_uber_page([301537], 1)]))
    job = jobs[0]
    assert job.source_job_id == "301537"
    assert job.company == "Uber"
    # The API returns HTML entities inside plain-text fields.
    assert job.department == "Engineer - Risk & Safety"
    assert job.city == "Sunnyvale" and job.country == "US"   # normalized from "USA"
    assert job.posted_at.startswith("2026-06-19")
    assert job.job_url.endswith("/careers/list/301537/")


@pytest.mark.asyncio
async def test_uber_postings_have_no_description() -> None:
    """Documented, not accidental: there is no detail RPC and the job page 406s,
    so downstream role filtering has only the title to work with."""
    jobs = await Uber().fetch(FakeFetcher(posts=[_uber_page([1], 1)]))
    assert jobs[0].description_text is None


@pytest.mark.asyncio
async def test_uber_a_failed_page_is_not_a_complete_crawl() -> None:
    jobs = await Uber().fetch(FakeFetcher(posts=[None]))
    assert jobs == [] and not is_complete(jobs)


@pytest.mark.asyncio
async def test_every_employer_source_reports_completeness_honestly() -> None:
    """`complete` gates source-wide closure, and an empty result never proves
    the employer stopped hiring — only that the crawl failed."""
    for source, fetcher in (
        (Amazon(), FakeFetcher(gets=[{"hits": 0, "jobs": []}])),
        (Apple(), FakeFetcher(headers={"X-Apple-CSRF-Token": "t"},
                              posts=[{"res": {"searchResults": [], "totalRecords": 0}}])),
        (Uber(), FakeFetcher(posts=[_uber_page([], 0)])),
    ):
        jobs = await source.fetch(fetcher)
        assert jobs == []
        assert not is_complete(jobs), f"{source.name} claimed an empty crawl was complete"
