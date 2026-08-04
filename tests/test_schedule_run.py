"""`jobscrape schedule-run`: the composite the dashboard's Runs page and
in-process scheduler both launch as one subprocess.

Never runs the real pipeline — every stage function is replaced with a stub
that records what `argparse.Namespace` it was called with, so these tests
verify the *sequencing and per-mode arguments* `cmd_schedule_run` builds,
not discovery/validation/fetch themselves (those have their own tests).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from jobscrape import cli


@pytest.fixture()
def recorder(monkeypatch):
    """Stub every stage `cmd_schedule_run` can call; record call order + args."""
    calls: list[tuple[str, object]] = []

    async def fake_discover(args):
        calls.append(("discover", args))
        return 0

    async def fake_validate(args):
        calls.append(("validate", args))
        return 0

    async def fake_fetch(args):
        calls.append(("fetch", args))
        return 0

    async def fake_apify_pull(args):
        calls.append(("apify-pull", args))
        return 0

    def fake_dedupe(args):
        calls.append(("dedupe", args))
        return 0

    def fake_stats(args):
        calls.append(("stats", args))
        return 0

    monkeypatch.setattr(cli, "cmd_discover", fake_discover)
    monkeypatch.setattr(cli, "cmd_validate", fake_validate)
    monkeypatch.setattr(cli, "cmd_fetch", fake_fetch)
    monkeypatch.setattr(cli, "cmd_apify_pull", fake_apify_pull)
    monkeypatch.setattr(cli, "cmd_dedupe", fake_dedupe)
    monkeypatch.setattr(cli, "cmd_stats", fake_stats)
    return calls


def _mode_args(mode: str) -> object:
    return cli.build_parser().parse_args(["schedule-run", "--mode", mode])


async def test_daily_mode_is_bounded_and_pulls_apify(recorder) -> None:
    rc = await cli.cmd_schedule_run(_mode_args("daily"))
    assert rc == 0
    stages = [name for name, _ in recorder]
    # No separate dedupe/stats here anymore: `apify-pull` (stubbed above)
    # chains its own now, same as `fetch` — schedule-run's own sequencing
    # stops once it hands off to whichever one runs last.
    assert stages == ["discover", "validate", "fetch", "apify-pull"]

    discover_args = dict(recorder)["discover"]
    assert discover_args.vector == ["ct", "github"]

    validate_args = dict(recorder)["validate"]
    assert validate_args.limit == 1500

    fetch_args = dict(recorder)["fetch"]
    assert fetch_args.since == (date.today() - timedelta(days=1)).isoformat()


async def test_weekly_mode_is_wider_and_skips_apify(recorder) -> None:
    rc = await cli.cmd_schedule_run(_mode_args("weekly"))
    assert rc == 0
    stages = [name for name, _ in recorder]
    # No apify-pull: weekly would just re-ingest the same runs daily already
    # did. No separate dedupe/stats either — `fetch` (stubbed above) chains
    # its own now.
    assert stages == ["discover", "validate", "fetch"]

    discover_args = dict(recorder)["discover"]
    assert discover_args.vector is None  # every vector, none named explicitly

    validate_args = dict(recorder)["validate"]
    assert validate_args.limit == 6000

    fetch_args = dict(recorder)["fetch"]
    assert fetch_args.since == (date.today() - timedelta(days=60)).isoformat()


async def test_full_mode_is_unbounded(recorder) -> None:
    rc = await cli.cmd_schedule_run(_mode_args("full"))
    assert rc == 0
    stages = [name for name, _ in recorder]
    assert stages == ["discover", "validate", "fetch", "apify-pull"]

    discover_args = dict(recorder)["discover"]
    assert discover_args.vector is None

    validate_args = dict(recorder)["validate"]
    assert validate_args.limit is None  # every unchecked board, not a capped slice

    fetch_args = dict(recorder)["fetch"]
    assert fetch_args.since is None and fetch_args.until is None


async def test_a_failing_step_stops_the_sequence(recorder, monkeypatch) -> None:
    async def failing_validate(args):
        return 2

    monkeypatch.setattr(cli, "cmd_validate", failing_validate)
    rc = await cli.cmd_schedule_run(_mode_args("full"))
    assert rc == 2
    stages = [name for name, _ in recorder]
    assert stages == ["discover"]  # fetch/apify-pull never ran
