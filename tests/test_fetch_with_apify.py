"""`jobscrape fetch --with-apify`: the dashboard's "Fetch jobs" card now also
pulls Apify and cross-source dedupes, as one click.

`cmd_apify_pull`/`cmd_dedupe` are stubbed here — this is about the
composition `cmd_fetch` does (same pattern `cmd_schedule_run` already uses:
a direct in-process call via `_step_args`, not a new subprocess), not about
what apify-pull or dedupe do internally (those have their own tests).
"""

from __future__ import annotations

from typing import Any

import pytest

from jobscrape import cli
from jobscrape.filters import FilterFunnel


@pytest.fixture()
def _no_op_fetch_boards(monkeypatch):
    """Every board reports "nothing new" with no network call, so these tests
    are only about what happens after the fetch loop itself finishes."""

    async def fake(*_args: Any, **_kwargs: Any) -> Any:
        return {"new": 0, "refreshed": 0}, FilterFunnel()

    monkeypatch.setattr(cli, "_fetch_boards", fake)


def _fetch_args(**kw: Any):
    import argparse

    defaults = dict(
        db=None, since=None, until=None, sources=["greenhouse"], companies=1,
        unchecked=0, all_boards=True, concurrency=2, all_roles=False,
        worldwide=False, all_workplaces=False, include_roles=None, include_qa=False,
        include_management=False, refetch_details=False, include_restricted=False,
        max_results=None, no_cache=True, with_apify=False,
    )
    return argparse.Namespace(**{**defaults, **kw})


async def test_with_apify_runs_apify_pull_then_dedupe(
    _isolated_db, _no_op_fetch_boards, monkeypatch,
) -> None:
    calls: list[str] = []

    async def fake_apify_pull(args):
        calls.append("apify-pull")
        assert args.last == 3
        return 0

    def fake_dedupe(args):
        calls.append("dedupe")
        assert args.backfill is True
        return 0

    monkeypatch.setattr(cli, "cmd_apify_pull", fake_apify_pull)
    monkeypatch.setattr(cli, "cmd_dedupe", fake_dedupe)

    rc = await cli.cmd_fetch(_fetch_args(with_apify=True))
    assert rc == 0
    assert calls == ["apify-pull", "dedupe"]


async def test_without_the_flag_neither_runs(
    _isolated_db, _no_op_fetch_boards, monkeypatch,
) -> None:
    calls: list[str] = []

    async def fake_apify_pull(args):
        calls.append("apify-pull")
        return 0

    def fake_dedupe(args):
        calls.append("dedupe")
        return 0

    monkeypatch.setattr(cli, "cmd_apify_pull", fake_apify_pull)
    monkeypatch.setattr(cli, "cmd_dedupe", fake_dedupe)

    rc = await cli.cmd_fetch(_fetch_args())  # with_apify defaults to False
    assert rc == 0
    assert calls == []


async def test_an_apify_pull_failure_stops_before_dedupe(
    _isolated_db, _no_op_fetch_boards, monkeypatch,
) -> None:
    calls: list[str] = []

    async def failing_apify_pull(args):
        calls.append("apify-pull")
        return 1

    def fake_dedupe(args):
        calls.append("dedupe")
        return 0

    monkeypatch.setattr(cli, "cmd_apify_pull", failing_apify_pull)
    monkeypatch.setattr(cli, "cmd_dedupe", fake_dedupe)

    rc = await cli.cmd_fetch(_fetch_args(with_apify=True))
    assert rc == 1
    assert calls == ["apify-pull"]
