"""Indeed adapter, against the app-API response shape.

The live app API is blocked at Indeed's edge on the ``indeed-api-key`` header
itself (measured — see the adapter's module docstring), so these run against a
captured payload. That is the point of keeping them: the parsing has to stay
correct for the day the route reopens, and the *default-off* behaviour has to
stay correct today, since a source that silently returns nothing on every run is
exactly the failure that hides itself.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

import pytest

from jobscrape.sources.base import is_complete
from jobscrape.sources.restricted.indeed import Indeed


def _entry(**over: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "source": {"name": "Employer"},
        "key": "abc123",
        "title": "Senior Backend Engineer",
        # Epoch MILLISECONDS — read as seconds these land in 1970.
        "datePublished": 1785283200000,
        "dateOnIndeed": 1785283200000,
        "description": {"html": "<p>Build services.</p><ul><li>Go</li></ul>"},
        "location": {
            "countryName": "United States", "countryCode": "US",
            "admin1Code": "CA", "city": "San Francisco", "postalCode": "94105",
            "formatted": {"short": "San Francisco, CA", "long": "San Francisco, CA 94105"},
        },
        "compensation": {
            "baseSalary": {"unitOfWork": "YEAR", "range": {"min": 180000, "max": 240000}},
            "currencyCode": "USD",
        },
        "attributes": [{"key": "CF3CP", "label": "Full-time"}],
        "employer": {"name": "Acme Corp", "relativeCompanyPageUrl": "/cmp/Acme"},
        "recruit": {"viewJobUrl": "https://www.indeed.com/rc/clk?jk=abc123",
                    "detailedSalary": "$180,000 - $240,000 a year",
                    "workSchedule": "Full-time"},
    }
    job.update(over)
    return {"trackingKey": "tk", "job": job}


class FakeFetcher:
    """Returns queued payloads in order, recording the GraphQL query text."""

    def __init__(self, *payloads: Any) -> None:
        self._payloads = list(payloads)
        self.queries: list[str] = []

    async def post_json(self, url: str, body: Any, **kw: Any) -> Any:
        self.queries.append(body["query"])
        return self._payloads.pop(0) if self._payloads else None


def _page(results: list[dict[str, Any]], cursor: str | None = None) -> dict[str, Any]:
    return {"data": {"jobSearch": {"pageInfo": {"nextCursor": cursor}, "results": results}}}


# --- parsing ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_parses_a_posting_end_to_end() -> None:
    jobs = await Indeed()._via_graphql(
        FakeFetcher(_page([_entry()])), ["backend engineer"], None, None, None
    )
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source_job_id == "abc123"
    assert job.title == "Senior Backend Engineer"
    assert job.company == "Acme Corp"
    assert job.job_url == "https://www.indeed.com/viewjob?jk=abc123"
    assert job.apply_url == "https://www.indeed.com/rc/clk?jk=abc123"
    assert job.city == "San Francisco" and job.state == "CA" and job.country == "US"
    assert job.salary_min == 180_000 and job.salary_max == 240_000
    assert job.salary_currency == "USD"
    assert "Build services" in job.description_text
    assert "Go" in job.description_text
    # Milliseconds must not be read as seconds.
    assert job.posted_at.startswith("2026-")


@pytest.mark.asyncio
async def test_hourly_pay_is_annualised_and_unknown_units_are_dropped() -> None:
    """A raw $30 stored beside real annual figures would corrupt every range."""
    hourly = _entry(compensation={
        "baseSalary": {"unitOfWork": "HOUR", "range": {"min": 60, "max": 80}},
        "currencyCode": "USD",
    })
    jobs = await Indeed()._via_graphql(FakeFetcher(_page([hourly])), ["x"], None, None, None)
    assert jobs[0].salary_min == 60 * 2080 and jobs[0].salary_max == 80 * 2080

    weird = _entry(key="w1", compensation={
        "baseSalary": {"unitOfWork": "PER_WIDGET", "range": {"min": 5000, "max": 6000}},
        "currencyCode": "USD",
    }, description={"html": "<p>No pay stated in the body.</p>"},
       recruit={"viewJobUrl": None, "detailedSalary": None, "workSchedule": None})
    jobs = await Indeed()._via_graphql(FakeFetcher(_page([weird])), ["x"], None, None, None)
    assert jobs[0].salary_min is None, "an unconvertible unit must yield no salary"


@pytest.mark.asyncio
async def test_estimated_pay_is_not_stored_as_if_the_employer_said_it() -> None:
    """`estimated` is Indeed's own model output, not a stated range."""
    est_only = _entry(
        compensation={
            "estimated": {
                "currencyCode": "USD",
                "baseSalary": {"unitOfWork": "YEAR", "range": {"min": 1, "max": 2}},
            },
            "currencyCode": "USD",
        },
        description={"html": "<p>No numbers here.</p>"},
        recruit={"viewJobUrl": None, "detailedSalary": None, "workSchedule": None},
    )
    jobs = await Indeed()._via_graphql(FakeFetcher(_page([est_only])), ["x"], None, None, None)
    assert jobs[0].salary_min is None


@pytest.mark.asyncio
async def test_remote_attribute_key_sets_is_remote() -> None:
    remote = _entry(attributes=[{"key": "DSQF7", "label": "Remote"}])
    jobs = await Indeed()._via_graphql(FakeFetcher(_page([remote])), ["x"], None, None, None)
    assert jobs[0].is_remote is True


@pytest.mark.asyncio
async def test_a_posting_with_no_employer_is_not_given_a_fake_company() -> None:
    """Falling back to the feed name would invent one giant employer."""
    anon = _entry(employer=None)
    jobs = await Indeed()._via_graphql(FakeFetcher(_page([anon])), ["x"], None, None, None)
    assert jobs[0].company == ""


# --- pagination & windowing ------------------------------------------------


@pytest.mark.asyncio
async def test_pagination_advances_by_cursor_and_stops_when_it_runs_out() -> None:
    fetcher = FakeFetcher(
        _page([_entry(key="a")], cursor="CUR1"),
        _page([_entry(key="b")], cursor="CUR2"),
        _page([_entry(key="c")], cursor=None),
    )
    jobs = await Indeed()._via_graphql(fetcher, ["x"], None, None, None)
    assert [j.source_job_id for j in jobs] == ["a", "b", "c"]
    assert len(fetcher.queries) == 3
    # The first call sends no cursor; each later one sends the previous page's.
    assert "cursor:" not in fetcher.queries[0]
    assert '"CUR1"' in fetcher.queries[1]
    assert '"CUR2"' in fetcher.queries[2]


@pytest.mark.asyncio
async def test_duplicate_keys_across_terms_collapse() -> None:
    fetcher = FakeFetcher(_page([_entry(key="dup")]), _page([_entry(key="dup")]))
    jobs = await Indeed()._via_graphql(fetcher, ["term one", "term two"], None, None, None)
    assert len(jobs) == 1


@pytest.mark.asyncio
async def test_until_excludes_postings_newer_than_the_window() -> None:
    future = int(datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp() * 1000)
    fetcher = FakeFetcher(_page([_entry(key="old"), _entry(key="new", datePublished=future)]))
    jobs = await Indeed()._via_graphql(fetcher, ["x"], None, date(2026, 8, 1), None)
    assert [j.source_job_id for j in jobs] == ["old"]


@pytest.mark.asyncio
async def test_since_becomes_a_server_side_date_filter() -> None:
    """Filtering server-side is what keeps the page budget from being wasted."""
    fetcher = FakeFetcher(_page([_entry()]))
    await Indeed()._via_graphql(fetcher, ["x"], date.today(), None, None)
    assert 'field: "dateOnIndeed"' in fetcher.queries[0]
    assert "attributes" not in fetcher.queries[0].split("filters:")[1][:200]


@pytest.mark.asyncio
async def test_a_dead_page_stops_that_term_without_killing_the_run() -> None:
    fetcher = FakeFetcher(None, _page([_entry(key="ok")]))
    jobs = await Indeed()._via_graphql(fetcher, ["bad term", "good term"], None, None, None)
    assert [j.source_job_id for j in jobs] == ["ok"]


@pytest.mark.asyncio
async def test_graphql_errors_stop_that_term() -> None:
    fetcher = FakeFetcher({"errors": [{"message": "nope"}]}, _page([_entry(key="ok")]))
    jobs = await Indeed()._via_graphql(fetcher, ["bad", "good"], None, None, None)
    assert [j.source_job_id for j in jobs] == ["ok"]


@pytest.mark.asyncio
async def test_the_result_is_never_marked_complete() -> None:
    """Relevance-ranked search samples the catalogue; it never enumerates it.

    If this ever reported complete, the closure sweep would retire every Indeed
    posting the term list happened not to surface.
    """
    jobs = await Indeed()._via_graphql(FakeFetcher(_page([_entry()])), ["x"], None, None, None)
    assert not is_complete(jobs)


# --- default routing -------------------------------------------------------


@pytest.mark.asyncio
async def test_app_api_is_off_unless_explicitly_enabled(monkeypatch) -> None:
    """It is blocked at Indeed's edge, so the default must not burn requests."""
    monkeypatch.delenv("INDEED_PUBLISHER_ID", raising=False)
    monkeypatch.delenv("INDEED_ALLOW_APP_API", raising=False)
    fetcher = FakeFetcher(_page([_entry()]))
    jobs = await Indeed().fetch(fetcher)
    assert jobs == []
    assert fetcher.queries == [], "no request may be sent on the default path"

    monkeypatch.setenv("INDEED_ALLOW_APP_API", "1")
    jobs = await Indeed().fetch(fetcher, queries=["x"])
    assert len(jobs) == 1


@pytest.mark.asyncio
async def test_a_publisher_key_wins_over_the_unofficial_route(monkeypatch) -> None:
    monkeypatch.setenv("INDEED_PUBLISHER_ID", "pub-123")
    monkeypatch.setenv("INDEED_ALLOW_APP_API", "1")

    seen: list[str] = []

    class PubFetcher:
        async def get_json(self, url: str, **kw: Any) -> Any:
            seen.append(url)
            return {"results": [{"jobkey": "p1", "jobtitle": "Dev", "company": "Acme",
                                 "url": "https://indeed.com/x", "date": "2026-07-20"}]}

        async def post_json(self, url: str, body: Any, **kw: Any) -> Any:
            raise AssertionError("must not touch the unofficial API when a key is set")

    jobs = await Indeed().fetch(PubFetcher(), queries=["x"])
    assert [j.source_job_id for j in jobs] == ["p1"]
    assert all("api.indeed.com" in u for u in seen)


def test_the_query_is_valid_graphql_shape() -> None:
    """A malformed interpolation would only show up as a live 400."""
    from jobscrape.sources.restricted.indeed import _QUERY, _gql_str

    text = _QUERY.format(
        what=_gql_str('a "quoted" term'), where=_gql_str("United States"),
        limit=100, cursor='cursor: "C1"', filters=Indeed()._filters(None),
    )
    assert text.count("{") == text.count("}"), "unbalanced braces"
    # The user term must be escaped, not concatenated — otherwise a quote in a
    # search term breaks out of the string and changes the query.
    assert '\\"quoted\\"' in text
    assert json.loads(text[text.index("what:") + 5:].split("\n")[0].strip())
