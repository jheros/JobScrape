"""Uber's careers API — the last of the direct employer adapters.

Its failure mode answers HTTP 200 and looks like data, so these tests are
mostly about telling working from merely-plausible: Uber paginates only when
``limit``/``page`` are top-level, and nested inside ``params`` it returns the
identical result set for every page, forever.

**The Amazon and Apple tests were here** until both companies were blocked and
their adapters deleted. Each guarded a similar trap — Apple ignored a wrong
location filter key and served the worldwide catalogue with a 200, Amazon
stopped serving rows past 10,000 without saying so — and `git log` has them if
either is ever unblocked.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobscrape.sources.base import is_complete
from jobscrape.sources.employers import Uber


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
async def test_the_employer_source_reports_completeness_honestly() -> None:
    """`complete` gates source-wide closure, and an empty result never proves
    the employer stopped hiring — only that the crawl failed."""
    for source, fetcher in (
        (Uber(), FakeFetcher(posts=[_uber_page([], 0)])),
    ):
        jobs = await source.fetch(fetcher)
        assert jobs == []
        assert not is_complete(jobs), f"{source.name} claimed an empty crawl was complete"
