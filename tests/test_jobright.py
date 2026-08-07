"""JobRight scraper/mapper: `isRemote` missing must stay unknown, not onsite.

`mapper.to_job` treats `record.get("remote")` as a real boolean signal
whenever it is not `None` -- and `scraper.flatten` used to default a missing
`isRemote` to `""`, which is not `None`, so `bool("")` produced a confirmed
`is_remote=False` for a posting JobRight simply never reported a work model
for. That false "confirmed onsite" signal is exactly the kind of evidence
`classify_workplace`'s last-resort tier trusts, filing the posting as onsite
and dropping it from a remote-only sweep.
"""

from __future__ import annotations

from typing import Any

from jobscrape.sources.jobright import mapper
from jobscrape.sources.jobright.scraper import collect, flatten


def _entry(**job_over: Any) -> dict[str, Any]:
    job = {
        "applyLink": "https://acme.com/careers/123",
        "jobTitle": "Backend Engineer",
        "jobId": "123",
        "jobLocation": "New York, NY",
        **job_over,
    }
    return {"jobResult": job, "companyResult": {"companyName": "Acme"}}


def test_flatten_leaves_remote_none_when_jobright_never_reported_it() -> None:
    record = flatten(_entry())
    assert record["remote"] is None


def test_flatten_keeps_a_real_isremote_value() -> None:
    assert flatten(_entry(isRemote=True))["remote"] is True
    assert flatten(_entry(isRemote=False))["remote"] is False


def test_mapper_reads_missing_remote_as_unknown_not_onsite() -> None:
    record = flatten(_entry())
    job = mapper.to_job(record)
    assert job is not None
    assert job.is_remote is None


def test_mapper_still_honours_a_real_false() -> None:
    record = flatten(_entry(isRemote=False))
    job = mapper.to_job(record)
    assert job is not None
    assert job.is_remote is False


# --- collect() must not let a browser hiccup abort the whole sweep ---------


def test_collect_survives_a_non_runtime_error_from_the_fetcher() -> None:
    """`fetch_page`/`search_page` raise `RuntimeError` for their own known
    failure modes, but the Selenium call underneath them can raise its own
    exception types (a JS timeout, a disconnected driver) that are not
    `RuntimeError` subclasses. `collect()` used to only catch `RuntimeError`,
    so one of those escaped the function entirely instead of being captured
    as a note -- aborting the whole multi-role sweep on the first browser
    hiccup, exactly what this function's docstring promises does not
    happen."""

    class _JsTimeout(Exception):
        """Stands in for a Selenium exception -- not a RuntimeError."""

    def flaky_fetcher(position: int, count: int) -> list[dict]:
        raise _JsTimeout("script timeout")

    entries, note = collect(flaky_fetcher, limit=10, page_size=5, label="role", delay=0)
    assert entries == []
    assert "script timeout" in note


def test_collect_still_stops_on_a_runtime_error() -> None:
    """The original, narrower behaviour must still work."""

    def api_error_fetcher(position: int, count: int) -> list[dict]:
        raise RuntimeError("API error: rate limited")

    entries, note = collect(api_error_fetcher, limit=10, page_size=5, label="role", delay=0)
    assert entries == []
    assert "rate limited" in note


def test_collect_pages_until_the_limit() -> None:
    calls = []

    def fetcher(position: int, count: int) -> list[dict]:
        calls.append((position, count))
        return [{"id": str(position)}] * count

    entries, note = collect(fetcher, limit=10, page_size=5, label="role", delay=0)
    assert len(entries) == 10
    assert note == ""
    assert calls == [(0, 5), (5, 5)]
