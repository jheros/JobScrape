"""USAJobs salary parsing: `RateIntervalCode` must be honoured, not ignored.

`PositionRemuneration`'s `MinimumRange`/`MaximumRange` were stored verbatim
regardless of `RateIntervalCode` ("Per Year" for most postings, but plenty
are "Per Hour" / "Per Day" / "Per Month") -- an hourly rate filed as if it
were an annual salary.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from jobscrape import config
from jobscrape.sources.usajobs import USAJobs


def _entry(min_range, max_range, rate_interval, **over):
    descriptor = {
        "PositionTitle": "Support Specialist",
        "OrganizationName": "Dept. of Example",
        "PositionURI": "https://usajobs.gov/job/1",
        "PositionRemuneration": [{
            "MinimumRange": min_range, "MaximumRange": max_range,
            "RateIntervalCode": rate_interval,
        }],
        **over,
    }
    return {"MatchedObjectId": "1", "MatchedObjectDescriptor": descriptor}


def test_per_year_is_left_alone() -> None:
    job = USAJobs()._parse(_entry(60000, 80000, "Per Year"))
    assert job is not None
    assert (job.salary_min, job.salary_max) == (60000, 80000)
    assert job.salary_currency == "USD"


def test_per_hour_is_annualised() -> None:
    job = USAJobs()._parse(_entry(25, 35, "Per Hour"))
    assert job is not None
    assert job.salary_min == 25 * 2080
    assert job.salary_max == 35 * 2080
    assert job.salary_currency == "USD"


def test_unrecognised_rate_interval_is_dropped_not_guessed() -> None:
    job = USAJobs()._parse(_entry(100, 200, "Fee Basis"))
    assert job is not None
    assert job.salary_min is None
    assert job.salary_max is None
    assert job.salary_currency is None


# --- closure scope: `DatePosted` silently caps at 60 days -------------------


class _FakeFetcher:
    """One empty (exhausted-on-first-page) result for every term."""

    async def get_json(self, url: str, **kw: Any) -> Any:
        return {"SearchResult": {"SearchResultItems": []}}


@pytest.mark.asyncio
async def test_a_since_window_within_60_days_is_reported_complete(monkeypatch) -> None:
    monkeypatch.setattr(config, "USAJOBS_API_KEY", "test-key")
    monkeypatch.setattr(config, "USAJOBS_EMAIL", "test@example.com")

    batch = await USAJobs().fetch(
        _FakeFetcher(), since=date.today() - timedelta(days=10), queries=["engineer"],
    )
    assert batch.complete is True


@pytest.mark.asyncio
async def test_a_since_window_past_60_days_is_never_reported_complete(monkeypatch) -> None:
    """USAJobs's `DatePosted` filter cannot search further back than 60
    days, so a `since` requesting a wider window silently narrows what the
    crawl actually covers. A posting older than 60 days but still inside
    the requested window is never even queried for -- `complete` gates
    source-wide closure, and reporting this crawl as exhaustive would close
    postings this run simply never looked at, not postings that are
    actually gone."""
    monkeypatch.setattr(config, "USAJOBS_API_KEY", "test-key")
    monkeypatch.setattr(config, "USAJOBS_EMAIL", "test@example.com")

    batch = await USAJobs().fetch(
        _FakeFetcher(), since=date.today() - timedelta(days=90), queries=["engineer"],
    )
    assert batch.complete is False


@pytest.mark.asyncio
async def test_no_since_at_all_is_unaffected(monkeypatch) -> None:
    """No `since` means no `DatePosted` filter is sent at all -- the 60-day
    cap never applies, so a clean exhausted crawl is still complete."""
    monkeypatch.setattr(config, "USAJOBS_API_KEY", "test-key")
    monkeypatch.setattr(config, "USAJOBS_EMAIL", "test@example.com")

    batch = await USAJobs().fetch(_FakeFetcher(), queries=["engineer"])
    assert batch.complete is True
