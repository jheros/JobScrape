"""Tests for the JSearch adapter.

Written against JSearch's documented schema without a live key, so these
fixtures encode the contract rather than a captured response. They exist mainly
to guarantee the two properties that matter without a key: it never fires a
request it cannot pay for, and an unexpected shape degrades instead of raising.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobscrape import config
from jobscrape.sources.jsearch import JSearch, _first


class FakeFetcher:
    def __init__(self, payload: Any = None) -> None:
        self._payload = payload
        self.calls = 0

    async def get_json(self, url: str, **kw: Any) -> Any:
        self.calls += 1
        return self._payload


def test_unavailable_without_a_key(monkeypatch) -> None:
    monkeypatch.setattr(config, "RAPIDAPI_KEY", "")
    ok, reason = JSearch().available()
    assert ok is False
    assert "RAPIDAPI_KEY" in reason


async def test_no_key_means_no_requests(monkeypatch) -> None:
    """A missing key must cost zero quota and zero requests."""
    monkeypatch.setattr(config, "RAPIDAPI_KEY", "")
    fetcher = FakeFetcher({"data": []})
    assert await JSearch().fetch(fetcher) == []
    assert fetcher.calls == 0


async def test_request_budget_is_enforced(monkeypatch) -> None:
    """The free tier is ~200 requests/month, so one run must not drain it."""
    monkeypatch.setattr(config, "RAPIDAPI_KEY", "test-key")
    monkeypatch.setattr(config, "JSEARCH_MAX_REQUESTS", 3)
    fetcher = FakeFetcher({"data": []})
    await JSearch().fetch(fetcher)
    assert fetcher.calls == 3, "must stop at the configured budget"


async def test_zero_budget_skips_entirely(monkeypatch) -> None:
    monkeypatch.setattr(config, "RAPIDAPI_KEY", "test-key")
    monkeypatch.setattr(config, "JSEARCH_MAX_REQUESTS", 0)
    fetcher = FakeFetcher({"data": []})
    assert await JSearch().fetch(fetcher) == []
    assert fetcher.calls == 0


async def test_parsing(monkeypatch) -> None:
    monkeypatch.setattr(config, "RAPIDAPI_KEY", "test-key")
    monkeypatch.setattr(config, "JSEARCH_MAX_REQUESTS", 1)
    payload = {
        "data": [
            {
                "job_id": "abc123",
                "job_title": "Senior Software Engineer",
                "employer_name": "Acme Inc",
                "job_apply_link": "https://example.com/apply/abc123",
                "job_city": "Austin", "job_state": "TX", "job_country": "US",
                "job_is_remote": True,
                "job_employment_type": "FULLTIME",
                "job_publisher": "LinkedIn",
                "job_posted_at_datetime_utc": "2026-07-20T12:00:00.000Z",
                "job_min_salary": 150000, "job_max_salary": 200000,
                "job_salary_currency": "USD", "job_salary_period": "YEAR",
                "job_description": "Build things.",
            }
        ]
    }
    jobs = await JSearch().fetch(FakeFetcher(payload))
    assert len(jobs) == 1
    job = jobs[0]
    assert job.company == "Acme Inc"
    assert job.state == "TX"
    assert job.workplace_type == "remote"
    assert job.salary_min == 150_000
    assert job.posted_at is not None and job.posted_at.startswith("2026-07-20")
    # Provenance: which board the listing was aggregated from.
    assert job.department == "LinkedIn"


async def test_hourly_salary_is_annualised(monkeypatch) -> None:
    """`job_salary_period` was never read, so an hourly `job_min_salary` was
    stored as if it were an annual figure -- $45-$55 filed as a $45-$55/year
    salary instead of the ~$94k-$114k it actually is."""
    monkeypatch.setattr(config, "RAPIDAPI_KEY", "test-key")
    monkeypatch.setattr(config, "JSEARCH_MAX_REQUESTS", 1)
    payload = {"data": [{
        "job_id": "hourly1", "job_title": "Support Engineer",
        "employer_name": "Acme Inc",
        "job_apply_link": "https://example.com/apply/hourly1",
        "job_min_salary": 45, "job_max_salary": 55,
        "job_salary_currency": "USD", "job_salary_period": "HOUR",
    }]}
    jobs = await JSearch().fetch(FakeFetcher(payload))
    assert len(jobs) == 1
    assert jobs[0].salary_min == 45 * 2080
    assert jobs[0].salary_max == 55 * 2080


async def test_salary_with_unrecognised_period_is_dropped_not_guessed() -> None:
    """An unrecognised period must not silently default to annual -- that
    would file a wrong number as if it were a real one."""
    from jobscrape.sources.jsearch import JSearch as _JS

    job = _JS()._parse({
        "job_id": "weird1", "job_title": "X",
        "job_min_salary": 100, "job_max_salary": 200,
        "job_salary_period": "FEE_BASIS",
    })
    assert job is not None
    assert job.salary_min is None
    assert job.salary_max is None
    assert job.salary_currency is None


async def test_unexpected_shape_degrades_rather_than_raises(monkeypatch) -> None:
    """The schema is unverified, so bad data must lose fields, not crash."""
    monkeypatch.setattr(config, "RAPIDAPI_KEY", "test-key")
    monkeypatch.setattr(config, "JSEARCH_MAX_REQUESTS", 1)
    payload = {"data": [
        {"job_id": "x", "job_title": "SWE"},          # almost everything missing
        {"no_id": True},                               # unusable, skipped
        "a bare string",                               # wrong type entirely
    ]}
    jobs = await JSearch().fetch(FakeFetcher(payload))
    assert len(jobs) == 1
    assert jobs[0].title == "SWE"


async def test_non_dict_response_is_survivable(monkeypatch) -> None:
    monkeypatch.setattr(config, "RAPIDAPI_KEY", "test-key")
    monkeypatch.setattr(config, "JSEARCH_MAX_REQUESTS", 1)
    assert await JSearch().fetch(FakeFetcher(None)) == []


def test_first_prefers_earlier_aliases() -> None:
    """Field names have changed between JSearch versions."""
    assert _first({"b": "second"}, "a", "b") == "second"
    assert _first({"a": "first", "b": "second"}, "a", "b") == "first"
    assert _first({"a": "", "b": "second"}, "a", "b") == "second"
    assert _first({}, "a", "b") is None
