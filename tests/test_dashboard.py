"""Dashboard analytics, charts and routes.

The web layer is thin by design, so most of the risk sits in the queries and in
the run controller. Both are tested without starting a server.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from jobscrape import config, db
from jobscrape.dashboard import analytics, charts
from jobscrape.dashboard.runner import STAGE_GROUPS, STAGES, RunManager
from jobscrape.models import Job


def _backdate_last_seen(conn: db.Connection, source_job_id: str, hours: float) -> None:
    """See tests/test_tracking.py — `mark_board_closures` only closes a
    posting once it has been unseen for `config.CLOSURE_GRACE_HOURS`, so
    exercising real closure needs a row that looks old, not just-upserted.
    """
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn.execute(
        "UPDATE jobs SET last_seen_at = ? WHERE source_job_id = ?", (stamp, source_job_id)
    )


@pytest.fixture()
def populated(tmp_path):
    path = tmp_path / "d.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [
            Job(source="greenhouse", source_job_id="1", title="Software Engineer",
                company="Acme", company_slug="acme", job_url="https://x/1",
                location_raw="Austin, TX", state="TX", posted_at="2026-07-01",
                salary_min=150000, salary_max=200000, salary_currency="USD",
                description_html="<p>Build</p>"),
            Job(source="greenhouse", source_job_id="2", title="Backend Engineer",
                company="Acme", company_slug="acme", job_url="https://x/2",
                location_raw="Remote - US", state=None, posted_at="2026-07-02"),
            Job(source="lever", source_job_id="3", title="Platform Engineer",
                company="Globex", company_slug="globex", job_url="https://x/3",
                location_raw="Austin, TX (Hybrid)", state="TX", posted_at="2026-07-03"),
        ])
        db.add_companies(conn, "greenhouse", ["acme", "other"])
        db.mark_company(conn, "greenhouse", "acme", "live", 2)
        yield conn, path


# --- analytics ------------------------------------------------------------

def test_overview_counts(populated) -> None:
    conn, _ = populated
    data = analytics.overview(conn)
    assert data["total_jobs"] == 3
    assert data["open_jobs"] == 3
    assert data["untriaged"] == 3
    assert data["sources"] == 2
    assert data["live_boards"] == 1


def test_triage_reduces_the_review_queue(populated) -> None:
    """The number the daily workflow depends on."""
    conn, _ = populated
    db.set_job_status(conn, "greenhouse", "1", "applied")
    assert analytics.overview(conn)["untriaged"] == 2


def test_closed_jobs_leave_the_open_count(populated) -> None:
    conn, _ = populated
    _backdate_last_seen(conn, "2", config.CLOSURE_GRACE_HOURS + 1)
    db.mark_board_closures(conn, "greenhouse", "acme", {"1"})
    data = analytics.overview(conn)
    assert data["open_jobs"] == 2 and data["closed_jobs"] == 1


def test_next_action_prioritises_by_leverage() -> None:
    empty = {"total_jobs": 0, "unchecked_boards": 0, "untriaged": 0}
    assert analytics.next_action(empty)["action"] == "sweep"

    backlog = {"total_jobs": 100, "unchecked_boards": 3000, "untriaged": 50}
    assert analytics.next_action(backlog)["action"] == "validate", (
        "a large unvalidated backlog outranks triage"
    )

    triage = {"total_jobs": 100, "unchecked_boards": 10, "untriaged": 50}
    assert analytics.next_action(triage)["action"] == "jobs"

    done = {"total_jobs": 100, "unchecked_boards": 10, "untriaged": 0}
    assert analytics.next_action(done)["action"] == "fetch"


def test_search_defaults_to_untriaged(populated) -> None:
    conn, _ = populated
    db.set_job_status(conn, "greenhouse", "1", "dismissed")
    rows, total = analytics.search_jobs(conn, status="new")
    assert total == 2
    assert all(r["source_job_id"] != "1" for r in rows)


def test_search_filters_combine(populated) -> None:
    conn, _ = populated
    _, total = analytics.search_jobs(conn, status="all", source="lever")
    assert total == 1
    _, total = analytics.search_jobs(conn, status="all", state="TX")
    assert total == 2
    _, total = analytics.search_jobs(conn, status="all", query="Backend")
    assert total == 1
    _, total = analytics.search_jobs(conn, status="all", min_salary=190000)
    assert total == 1


def test_search_hides_closed_by_default(populated) -> None:
    conn, _ = populated
    _backdate_last_seen(conn, "2", config.CLOSURE_GRACE_HOURS + 1)
    db.mark_board_closures(conn, "greenhouse", "acme", {"1"})
    _, hidden = analytics.search_jobs(conn, status="all")
    _, shown = analytics.search_jobs(conn, status="all", include_closed=True)
    assert hidden == 2 and shown == 3


def test_timeline_has_no_gaps(populated) -> None:
    """Missing days must render as zero, not vanish and distort the axis."""
    conn, _ = populated
    series = analytics.jobs_over_time(conn, days=10)
    assert len(series) == 11
    assert all("date" in p and "count" in p for p in series)


def test_coverage_funnel(populated) -> None:
    conn, _ = populated
    rows = analytics.coverage(conn)
    gh = next(r for r in rows if r["ats"] == "greenhouse")
    assert gh["discovered"] == 2 and gh["live"] == 1 and gh["unchecked"] == 1


def test_run_history_parses_stats(populated) -> None:
    conn, _ = populated
    run_id = db.start_run(conn, "fetch", "fetch")
    db.finish_run(conn, run_id, "ok", {"rows_touched": 42})
    history = analytics.run_history(conn)
    assert history[0]["stats_parsed"]["rows_touched"] == 42
    assert history[0]["duration"]


def test_analytics_survive_an_empty_database(tmp_path) -> None:
    """A brand-new install must render, not crash."""
    with db.session(tmp_path / "empty.db") as conn:
        data = analytics.overview(conn)
        assert data["total_jobs"] == 0
        assert analytics.next_action(data)["action"] == "sweep"
        assert analytics.by_source(conn) == []
        assert analytics.latest_funnel(conn) == []
        rows, total = analytics.search_jobs(conn)
        assert rows == [] and total == 0


# --- charts ---------------------------------------------------------------

def test_charts_emit_svg() -> None:
    series = [{"date": "2026-07-01", "count": 5}, {"date": "2026-07-02", "count": 9}]
    svg = charts.area_chart(series)
    assert svg.startswith("<svg") and "var(--series-1)" in svg
    assert 'role="img"' in svg and "aria-label" in svg


def test_charts_degrade_to_an_empty_state() -> None:
    assert "chart-empty" in charts.area_chart([])
    assert "chart-empty" in charts.bar_chart([], label_key="a", value_key="b")
    assert "chart-empty" in charts.stacked_bars([], label_key="a", segments=[("x", "X")])


def test_stacked_bars_label_segments() -> None:
    """Direct labels are the relief the light-mode contrast warning requires."""
    rows = [{"ats": "greenhouse", "live": 1000, "unchecked": 800}]
    svg = charts.stacked_bars(
        rows, label_key="ats", segments=[("live", "Live"), ("unchecked", "Unchecked")]
    )
    assert "seg-value" in svg, "wide segments must carry a visible value"
    assert "<title>" in svg, "every segment needs a hover tooltip"


def test_series_slots_are_never_cycled() -> None:
    """A fifth category must not silently reuse slot 1's hue."""
    assert len(charts.SERIES) == 4
    assert len(set(charts.SERIES)) == 4


def test_stacked_bars_colors_override_by_key_not_position() -> None:
    """Coverage's live/unchecked/empty/dead needs a specific hue per segment
    regardless of where it falls in the stack — e.g. "live" must render
    green even though it's declared first (stacking position 0, which
    defaults to the blue slot)."""
    rows = [{"ats": "greenhouse", "live": 500, "dead": 100}]
    svg = charts.stacked_bars(
        rows, label_key="ats", segments=[("live", "Live"), ("dead", "Dead")],
        colors={"live": "var(--series-3)", "dead": "var(--series-2)"},
    )
    assert 'fill="var(--series-3)"' in svg
    assert 'fill="var(--series-2)"' in svg
    assert 'fill="var(--series-1)"' not in svg  # the default position-0 slot


def test_stacked_bars_without_colors_keeps_positional_default() -> None:
    rows = [{"ats": "greenhouse", "live": 500, "dead": 100}]
    svg = charts.stacked_bars(
        rows, label_key="ats", segments=[("live", "Live"), ("dead", "Dead")],
    )
    assert 'fill="var(--series-1)"' in svg  # position 0, unchanged behaviour


def test_legend_is_present_for_multiple_series() -> None:
    out = charts.legend([("a", "Alpha"), ("b", "Beta")])
    assert out.count("legend-item") == 2
    assert "Alpha" in out and "Beta" in out


def test_legend_colors_match_stacked_bars_when_given() -> None:
    out = charts.legend(
        [("live", "Live"), ("dead", "Dead")],
        colors={"live": "var(--series-3)", "dead": "var(--series-2)"},
    )
    assert "background:var(--series-3)" in out
    assert "background:var(--series-2)" in out


def test_chart_escapes_untrusted_labels() -> None:
    """Company names land in SVG; they must not be able to inject markup."""
    rows = [{"company": '<script>alert(1)</script>', "c": 5}]
    svg = charts.bar_chart(rows, label_key="company", value_key="c")
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


# --- runner ---------------------------------------------------------------

def test_stage_allowlist_prevents_arbitrary_commands() -> None:
    """The web layer must never be able to assemble its own command line."""
    assert set(STAGES) == {
        "discover", "discover-fast", "validate", "fetch", "fetch-with-apify",
        "export", "apify-pull", "dedupe", "purge", "liveness", "stats", "sources",
        "daily-schedule", "weekly-schedule", "full-sweep", "jobright-fetch",
        "render-sync",
    }
    known_subcommands = {
        "discover", "validate", "fetch", "export", "apify-pull", "dedupe",
        "purge", "liveness", "stats", "sources", "schedule-run", "jobright-fetch",
        "render-sync",
    }
    for argv in STAGES.values():
        assert argv[0] in known_subcommands


async def test_unknown_stage_is_rejected(tmp_path) -> None:
    manager = RunManager(str(tmp_path / "r.db"))
    with pytest.raises(ValueError):
        await manager.start("rm -rf /")


def test_stage_groups_only_reference_real_stages() -> None:
    """A composite's group must be plain, launchable stages, and never
    reference the composite itself or another composite."""
    for composite, members in STAGE_GROUPS.items():
        assert composite in STAGES
        for member in members:
            assert member in STAGES
            assert member not in STAGE_GROUPS, "a composite's group must be flat"


async def test_composite_blocks_its_own_members(tmp_path, monkeypatch) -> None:
    """Starting a composite must reserve every plain stage in its group, so a
    manual click on one of them mid-run is refused rather than racing it."""
    manager = RunManager(str(tmp_path / "r.db"))

    class _FakeProcess:
        pid = 4242
        stdout = None

        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(manager, "_consume", lambda run: asyncio.sleep(0))

    await manager.start("daily-schedule")
    assert manager.is_running("discover")
    assert manager.is_running("fetch")
    with pytest.raises(RuntimeError):
        await manager.start("discover")
    # The composite itself must still show as one run, not six.
    assert len(manager.active()) == 1


async def test_fetch_with_apify_blocks_its_own_members(tmp_path, monkeypatch) -> None:
    """The dashboard's "Fetch jobs" card runs `fetch-with-apify`, which
    internally also touches `apify-pull` and `dedupe` (see `cmd_fetch`'s
    `--with-apify` handling) — a manual click on either while it's running
    must be refused, same as any other composite's members."""
    manager = RunManager(str(tmp_path / "r.db"))

    class _FakeProcess:
        pid = 4244
        stdout = None

        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(manager, "_consume", lambda run: asyncio.sleep(0))

    await manager.start("fetch-with-apify")
    assert manager.is_running("fetch")
    assert manager.is_running("apify-pull")
    assert manager.is_running("dedupe")
    with pytest.raises(RuntimeError):
        await manager.start("apify-pull")
    with pytest.raises(RuntimeError):
        await manager.start("dedupe")
    assert len(manager.active()) == 1


async def test_stopping_a_composite_frees_its_whole_group(tmp_path, monkeypatch) -> None:
    manager = RunManager(str(tmp_path / "r.db"))

    class _FakeStdout:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _FakeProcess:
        pid = 4243
        stdout = _FakeStdout()

        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    # `start()` spawns its own consumer task; replaced with a no-op so it
    # cannot race the explicit, deterministic `_consume` call below.
    monkeypatch.setattr(manager, "_consume", lambda run: asyncio.sleep(0))

    await manager.start("weekly-schedule")
    run = manager._active["weekly-schedule"]
    # Bypass the instance-level monkeypatch to run the REAL cleanup logic once.
    await RunManager._consume(manager, run)
    assert not manager.is_running("discover")
    assert not manager.is_running("weekly-schedule")
    assert manager.active() == []


async def test_window_appends_since_and_until_to_argv(tmp_path, monkeypatch) -> None:
    """A `window` is built only from already-validated `date` objects (the
    dashboard route parses them with `filters.parse_date` first) — this just
    checks `start()` turns that into the right trailing argv."""
    from datetime import date

    manager = RunManager(str(tmp_path / "r.db"))

    class _FakeProcess:
        pid = 5001
        stdout = None

        async def wait(self):
            return 0

    captured: dict[str, tuple] = {}

    async def fake_exec(*args, **kwargs):
        captured["argv"] = args
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(manager, "_consume", lambda run: asyncio.sleep(0))

    await manager.start("fetch", window=(date(2026, 7, 1), date(2026, 7, 31)))
    assert captured["argv"][-4:] == ("--since", "2026-07-01", "--until", "2026-07-31")


async def test_window_with_only_since_omits_until(tmp_path, monkeypatch) -> None:
    from datetime import date

    manager = RunManager(str(tmp_path / "r.db"))

    class _FakeProcess:
        pid = 5002
        stdout = None

        async def wait(self):
            return 0

    captured: dict[str, tuple] = {}

    async def fake_exec(*args, **kwargs):
        captured["argv"] = args
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(manager, "_consume", lambda run: asyncio.sleep(0))

    await manager.start("fetch", window=(date(2026, 7, 1), None))
    assert captured["argv"][-2:] == ("--since", "2026-07-01")
    assert "--until" not in captured["argv"]


def test_progress_parsing() -> None:
    manager = RunManager()
    from jobscrape.dashboard.runner import ActiveRun

    run = ActiveRun(run_id=1, stage="fetch", command="fetch")
    manager._parse(run, "12:00 INFO greenhouse       fetching 1343 boards...")
    assert run.progress["boards_total"] == 1343
    assert run.progress["current"] == "greenhouse"

    manager._parse(
        run,
        "12:05 INFO greenhouse       done: 1343 boards, 72949 postings seen, "
        "7482 matched, 7482 new, 0 refreshed, 0 closed",
    )
    assert run.progress["sources"]["greenhouse"]["new"] == 7482
    assert run.progress["jobs_new"] == 7482

    manager._parse(run, "12:06 INFO   ashby: 950/1566 probed (live=814 empty=40 dead=96)")
    assert run.progress["boards_done"] == 950


def test_progress_parsing_ignores_unrelated_lines() -> None:
    manager = RunManager()
    from jobscrape.dashboard.runner import ActiveRun

    run = ActiveRun(run_id=1, stage="fetch", command="fetch")
    assert manager._parse(run, "12:00 INFO something entirely unrelated") is None
    assert run.progress == {}
