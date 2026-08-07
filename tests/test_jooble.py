"""Jooble salary parsing: an hourly rate must not be filed as an annual one.

`salary` arrives as free prose ("$25 - $35 per hour", "$90,000 - $120,000 a
year") with no separate period field. A hand-rolled regex used to pull the
two numbers out and store them verbatim regardless of period; the fix relies
on `Job.__post_init__`'s own `models.parse_salary` fallback (run against
`salary_raw` whenever `salary_min` is left blank), which annualises
correctly.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobscrape import config
from jobscrape.sources.jooble import Jooble


class FakeFetcher:
    def __init__(self, *payloads: Any) -> None:
        self._payloads = list(payloads)

    async def post_json(self, url: str, body: Any, **kw: Any) -> Any:
        return self._payloads.pop(0) if self._payloads else None


def _posting(**over: Any) -> dict[str, Any]:
    item = {
        "id": "1", "title": "Backend Engineer", "company": "Acme",
        "link": "https://acme.com/jobs/1", "location": "Austin, TX",
        "salary": "$25 - $35 per hour", "type": "Full-time",
        "updated": "2026-07-01",
    }
    item.update(over)
    return item


@pytest.mark.asyncio
async def test_hourly_salary_is_annualised(monkeypatch) -> None:
    monkeypatch.setattr(config, "JOOBLE_API_KEY", "test-key")
    fetcher = FakeFetcher({"jobs": [_posting()]}, {"jobs": []})
    jobs = await Jooble().fetch(fetcher, queries=["engineer"])
    assert len(jobs) == 1
    assert jobs[0].salary_min == 25 * 2080
    assert jobs[0].salary_max == 35 * 2080


@pytest.mark.asyncio
async def test_annual_salary_is_left_alone(monkeypatch) -> None:
    monkeypatch.setattr(config, "JOOBLE_API_KEY", "test-key")
    fetcher = FakeFetcher(
        {"jobs": [_posting(salary="$90,000 - $120,000 a year")]}, {"jobs": []},
    )
    jobs = await Jooble().fetch(fetcher, queries=["engineer"])
    assert len(jobs) == 1
    assert jobs[0].salary_min == 90_000
    assert jobs[0].salary_max == 120_000


@pytest.mark.asyncio
async def test_no_salary_text_yields_no_salary(monkeypatch) -> None:
    monkeypatch.setattr(config, "JOOBLE_API_KEY", "test-key")
    fetcher = FakeFetcher({"jobs": [_posting(salary=None)]}, {"jobs": []})
    jobs = await Jooble().fetch(fetcher, queries=["engineer"])
    assert len(jobs) == 1
    assert jobs[0].salary_min is None
    assert jobs[0].salary_max is None
