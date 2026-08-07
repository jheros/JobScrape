"""`cmd_fetch` always chains dedupe then stats now, unconditionally — every
fetch-type command does (see `cmd_apify_pull`/`cmd_jobright_fetch` for the
same pattern; those have their own tests). Formerly this was gated behind a
now-removed `--with-apify` flag that also pulled Apify; Apify is its own
command with its own date window now, not something `cmd_fetch` touches.

`cmd_dedupe`/`cmd_stats` are stubbed here — this is about the composition
`cmd_fetch` does (the same in-process `_step_args` pattern `cmd_schedule_run`
uses, not a new subprocess), not about what dedupe/stats do internally.
"""

from __future__ import annotations

import json
from typing import Any

from jobscrape import cli
from jobscrape.filters import FilterFunnel


import pytest


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
        max_results=None, no_cache=True,
    )
    return argparse.Namespace(**{**defaults, **kw})


async def test_fetch_chains_dedupe_then_stats(
    _isolated_db, _no_op_fetch_boards, monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_dedupe(args):
        calls.append("dedupe")
        assert args.backfill is True
        return 0

    def fake_stats(args):
        calls.append("stats")
        return 0

    monkeypatch.setattr(cli, "cmd_dedupe", fake_dedupe)
    monkeypatch.setattr(cli, "cmd_stats", fake_stats)

    rc = await cli.cmd_fetch(_fetch_args())
    assert rc == 0
    assert calls == ["dedupe", "stats"]


async def test_a_dedupe_failure_stops_before_stats(
    _isolated_db, _no_op_fetch_boards, monkeypatch,
) -> None:
    calls: list[str] = []

    def failing_dedupe(args):
        calls.append("dedupe")
        return 1

    def fake_stats(args):
        calls.append("stats")
        return 0

    monkeypatch.setattr(cli, "cmd_dedupe", failing_dedupe)
    monkeypatch.setattr(cli, "cmd_stats", fake_stats)

    rc = await cli.cmd_fetch(_fetch_args())
    assert rc == 1
    assert calls == ["dedupe"]


# --- jobright-ingest: the one hand-ingestion route that used to skip this ---
#
# `cmd_apify_pull` and `cmd_jobright_fetch` both chain dedupe+stats after
# ingesting; `cmd_jobright_ingest` (the low-level "ingest a JSON array by
# hand" primitive, also the backfill route for an old export) did not — a
# hand-ingested batch never got deduped against the rest of the archive,
# unlike every other ingestion path.


def _jobright_ingest_args(tmp_path, records, **kw: Any):
    import argparse

    payload = tmp_path / "records.json"
    payload.write_text(json.dumps(records), encoding="utf-8")
    defaults = dict(
        db=None, file=str(payload), max_results=None,
        all_roles=True, worldwide=True, all_workplaces=True,
    )
    return argparse.Namespace(**{**defaults, **kw})


_JOBRIGHT_RECORD = {
    "jobId": "1", "positionName": "Backend Engineer", "companyName": "Acme",
    "url": "https://acme.com/careers/1",
}


def test_jobright_ingest_chains_dedupe_then_stats(
    _isolated_db, tmp_path, monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_dedupe(args):
        calls.append("dedupe")
        assert args.backfill is True
        return 0

    def fake_stats(args):
        calls.append("stats")
        return 0

    monkeypatch.setattr(cli, "cmd_dedupe", fake_dedupe)
    monkeypatch.setattr(cli, "cmd_stats", fake_stats)

    rc = cli.cmd_jobright_ingest(_jobright_ingest_args(tmp_path, [_JOBRIGHT_RECORD]))
    assert rc == 0
    assert calls == ["dedupe", "stats"]


def test_jobright_ingest_dedupe_failure_stops_before_stats(
    _isolated_db, tmp_path, monkeypatch,
) -> None:
    calls: list[str] = []

    def failing_dedupe(args):
        calls.append("dedupe")
        return 1

    def fake_stats(args):
        calls.append("stats")
        return 0

    monkeypatch.setattr(cli, "cmd_dedupe", failing_dedupe)
    monkeypatch.setattr(cli, "cmd_stats", fake_stats)

    rc = cli.cmd_jobright_ingest(_jobright_ingest_args(tmp_path, [_JOBRIGHT_RECORD]))
    assert rc == 1
    assert calls == ["dedupe"]
