"""Closure detection, run history, and your triage decisions.

Two failure modes here are severe and silent:

* Inferring closures from a *failed* board fetch would wipe a company's whole
  listing because of one network error. `mark_board_closures` trusts its
  caller on this rather than guessing from `seen_ids` itself — an EMPTY
  `seen_ids` is taken at face value as "this board now has zero postings",
  not as a sign the fetch failed, so it is `cli.py`'s job (its `jobs is
  None`/`is_complete` checks upstream of both call sites) to keep a failed
  fetch from ever reaching this function at all.
* Letting a re-fetch overwrite job_status would erase the record of where you
  applied.

Both are guarded explicitly below.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from jobscrape import config, db
from jobscrape.filters import FilterFunnel
from jobscrape.models import Job


def _job(job_id: str, slug: str = "acme", **kw: object) -> Job:
    base = dict(
        source="ashby", source_job_id=job_id, title="Software Engineer",
        company="Acme", company_slug=slug, job_url=f"https://x/{job_id}",
    )
    base.update(kw)
    return Job(**base)  # type: ignore[arg-type]


def _backdate_last_seen(conn: db.Connection, source_job_id: str, hours: float) -> None:
    """Simulate a posting last confirmed present ``hours`` ago.

    `mark_board_closures` only closes a posting once it has been unseen for
    `config.CLOSURE_GRACE_HOURS` — a fresh `upsert_jobs` always stamps
    `last_seen_at` as now, so exercising the actual closure path (rather than
    just the grace guard) needs a row that looks old.
    """
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn.execute(
        "UPDATE jobs SET last_seen_at = ? WHERE source_job_id = ?", (stamp, source_job_id)
    )


# --- closure detection ----------------------------------------------------

def test_recently_seen_absent_posting_stays_open_during_grace(tmp_path) -> None:
    """A posting missing from one crawl must not close immediately.

    Boards paginate inconsistently and truncate under load — closing on the
    first absence would wrongly close postings that are still live. See
    `config.CLOSURE_GRACE_HOURS`.
    """
    with db.session(tmp_path / "c.db") as conn:
        db.upsert_jobs(conn, [_job("a"), _job("b"), _job("c")])
        # A successful fetch that no longer lists "c" — but "c" was only just
        # seen, so it's within the grace window.
        closed = db.mark_board_closures(conn, "ashby", "acme", {"a", "b"})
        assert closed == 0
        rows = {r["source_job_id"]: r["closed_at"]
                for r in conn.execute("SELECT source_job_id, closed_at FROM jobs")}
        assert rows == {"a": None, "b": None, "c": None}


def test_absent_posting_closes_once_grace_elapses(tmp_path) -> None:
    with db.session(tmp_path / "c.db") as conn:
        db.upsert_jobs(conn, [_job("a"), _job("b"), _job("c")])
        _backdate_last_seen(conn, "c", config.CLOSURE_GRACE_HOURS + 1)

        closed = db.mark_board_closures(conn, "ashby", "acme", {"a", "b"})
        assert closed == 1
        rows = {r["source_job_id"]: r["closed_at"] is not None
                for r in conn.execute("SELECT source_job_id, closed_at FROM jobs")}
        assert rows == {"a": False, "b": False, "c": True}


def test_empty_seen_ids_closes_everything_past_grace() -> None:
    """A board that genuinely dropped to zero postings must actually close.

    `mark_board_closures` cannot tell "the fetch failed" apart from "the
    board answered with nothing" by looking at `seen_ids` alone — that used
    to be guessed here (an early return on an empty set), which meant a
    board that had genuinely gone to zero could never close: every one of
    its postings sat open forever. The guard against a *failed* fetch lives
    at the call site instead (`cli.py` never calls this unless the fetch
    itself succeeded) — see `test_a_failed_fetch_never_reaches_mark_board_closures`
    below for that half of the contract.
    """
    with db.session() as conn:
        db.upsert_jobs(conn, [_job("a"), _job("b")])
        _backdate_last_seen(conn, "a", config.CLOSURE_GRACE_HOURS + 1)
        _backdate_last_seen(conn, "b", config.CLOSURE_GRACE_HOURS + 1)
        assert db.mark_board_closures(conn, "ashby", "acme", set()) == 2
        still_open = conn.execute(
            "SELECT COUNT(*) c FROM jobs WHERE closed_at IS NULL"
        ).fetchone()["c"]
        assert still_open == 0


def test_a_failed_fetch_never_reaches_mark_board_closures() -> None:
    """The other half of the contract above: `cli.py`'s dispatch must treat
    `jobs is None` (the fetch itself failed) as never calling
    `mark_board_closures` at all -- it is not this function's job to
    re-derive that from an empty `seen_ids`, since a genuinely-empty
    successful board is indistinguishable from a failure by that signal
    alone (see the test above)."""
    import inspect

    from jobscrape import cli

    source = inspect.getsource(cli._fetch_boards)
    none_check = source.index("if jobs is None:")
    closure_call = source.index("db.mark_board_closures(")
    assert none_check < closure_call, (
        "the `jobs is None` guard must appear (and therefore run, and "
        "return) before the mark_board_closures call in the same function"
    )


def test_closures_are_scoped_to_one_board(tmp_path) -> None:
    """Fetching one company must never close another company's jobs."""
    with db.session(tmp_path / "c.db") as conn:
        db.upsert_jobs(conn, [_job("a", "acme"), _job("z", "globex")])
        _backdate_last_seen(conn, "a", config.CLOSURE_GRACE_HOURS + 1)
        db.mark_board_closures(conn, "ashby", "acme", {"other"})
        other = conn.execute(
            "SELECT closed_at FROM jobs WHERE source_job_id = 'z'"
        ).fetchone()["closed_at"]
        assert other is None


def test_reopened_posting_is_not_reclosed(tmp_path) -> None:
    """A closed job that comes back through a fresh upsert reopens automatically."""
    with db.session(tmp_path / "c.db") as conn:
        db.upsert_jobs(conn, [_job("a")])
        _backdate_last_seen(conn, "a", config.CLOSURE_GRACE_HOURS + 1)
        db.mark_board_closures(conn, "ashby", "acme", {"other"})
        assert conn.execute(
            "SELECT closed_at FROM jobs"
        ).fetchone()["closed_at"] is not None

        # Listed again: upsert_jobs clears closed_at unconditionally — a
        # posting the source is actively serving again is not closed,
        # regardless of what the last sweep concluded.
        db.upsert_jobs(conn, [_job("a")])
        assert conn.execute("SELECT closed_at FROM jobs").fetchone()["closed_at"] is None

        # And a closure pass right after must not immediately re-close it —
        # it was just re-seen, so it's back within its grace window.
        db.mark_board_closures(conn, "ashby", "acme", set())
        assert conn.execute("SELECT closed_at FROM jobs").fetchone()["closed_at"] is None


# --- job status -----------------------------------------------------------

def test_status_survives_a_refetch(tmp_path) -> None:
    """The whole reason job_status is a separate table."""
    with db.session(tmp_path / "s.db") as conn:
        db.upsert_jobs(conn, [_job("a")])
        db.set_job_status(conn, "ashby", "a", "applied", notes="referred by Sam")

        # A later sweep rewrites the job row entirely.
        db.upsert_jobs(conn, [_job("a", title="Senior Software Engineer")])

        row = conn.execute(
            "SELECT status, notes FROM job_status WHERE source_job_id = 'a'"
        ).fetchone()
        assert row["status"] == "applied"
        assert row["notes"] == "referred by Sam"
        assert conn.execute("SELECT title FROM jobs").fetchone()["title"] == (
            "Senior Software Engineer"
        )


def test_status_update_keeps_notes_when_omitted(tmp_path) -> None:
    with db.session(tmp_path / "s.db") as conn:
        db.set_job_status(conn, "ashby", "a", "saved", notes="looks promising")
        db.set_job_status(conn, "ashby", "a", "applied")
        row = conn.execute("SELECT status, notes FROM job_status").fetchone()
        assert row["status"] == "applied"
        assert row["notes"] == "looks promising"


def test_unknown_status_is_rejected(tmp_path) -> None:
    with db.session(tmp_path / "s.db") as conn:
        with pytest.raises(ValueError):
            db.set_job_status(conn, "ashby", "a", "definitely-not-a-status")


def test_untriaged_jobs_count_as_new(tmp_path) -> None:
    with db.session(tmp_path / "s.db") as conn:
        db.upsert_jobs(conn, [_job("a"), _job("b"), _job("c")])
        db.set_job_status(conn, "ashby", "a", "applied")
        counts = db.status_counts(conn)
        assert counts["applied"] == 1
        assert counts["new"] == 2, "never-triaged jobs are implicitly new"


# --- run history ----------------------------------------------------------

def test_run_lifecycle(tmp_path) -> None:
    with db.session(tmp_path / "r.db") as conn:
        run_id = db.start_run(conn, "fetch --since 2026-07-01", "fetch", pid=1234)
        db.log_event(conn, run_id, "greenhouse done", stage="fetch",
                     source="greenhouse", counters={"new": 42})
        db.finish_run(conn, run_id, "ok", {"jobs": 42})

        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["status"] == "ok"
        assert row["ended_at"] and row["pid"] == 1234
        # `stats` is JSONB — psycopg2 hands it back already deserialized.
        assert row["stats"] == {"jobs": 42}

        events = db.run_events(conn, run_id)
        assert len(events) == 1 and events[0]["source"] == "greenhouse"


def test_finish_run_merges_stats_instead_of_replacing_them(tmp_path) -> None:
    """A composite invocation shares one row across stages via
    `JOBSCRAPE_RUN_ID` — `schedule-run`'s `daily`/`full` modes have
    `cmd_fetch` and `cmd_apify_pull` each call `finish_run` once on the SAME
    run id, with disjoint stats keys. A blind overwrite meant whichever
    stage finished last silently erased the other's stats from the row."""
    with db.session(tmp_path / "r.db") as conn:
        run_id = db.start_run(conn, "schedule-run --mode daily", "fetch")
        db.finish_run(conn, run_id, "ok", {"sources": {"greenhouse": 5}, "rows_touched": 5})
        db.finish_run(conn, run_id, "ok", {"items": 12, "inserted": 3})

        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["stats"] == {
            "sources": {"greenhouse": 5}, "rows_touched": 5,
            "items": 12, "inserted": 3,
        }


def test_finish_run_merge_lets_a_later_call_overwrite_a_shared_key(tmp_path) -> None:
    """Not an append-only log — a key both calls set (e.g. a second stage
    correcting an earlier estimate) takes the later value, same as a plain
    dict `.update()`."""
    with db.session(tmp_path / "r.db") as conn:
        run_id = db.start_run(conn, "fetch", "fetch")
        db.finish_run(conn, run_id, "ok", {"rows_touched": 5})
        db.finish_run(conn, run_id, "ok", {"rows_touched": 9})

        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["stats"] == {"rows_touched": 9}


def test_stale_runs_are_closed_out(tmp_path) -> None:
    """A crashed run must not leave a phantom 'running' row forever."""
    with db.session(tmp_path / "r.db") as conn:
        db.start_run(conn, "fetch", "fetch")
        db.start_run(conn, "validate", "validate")
        assert db.mark_stale_runs(conn) == 2
        assert conn.execute(
            "SELECT COUNT(*) c FROM runs WHERE status = 'running'"
        ).fetchone()["c"] == 0


def test_run_events_paginate_from_a_cursor(tmp_path) -> None:
    """The live log tails by id, so it must not re-send what it has."""
    with db.session(tmp_path / "r.db") as conn:
        run_id = db.start_run(conn, "fetch", "fetch")
        for i in range(5):
            db.log_event(conn, run_id, f"line {i}")
        conn.commit()
        first = db.run_events(conn, run_id)
        assert len(first) == 5
        rest = db.run_events(conn, run_id, after_id=first[2]["id"])
        assert [e["message"] for e in rest] == ["line 3", "line 4"]


# --- a scheduled run has to close its own row, even when it crashes ---------


def _fetch_args(**kw: object):
    """The `cmd_fetch` namespace, with every stage disabled but the loop."""
    import argparse

    defaults = dict(
        db=None, since=None, until=None, sources=["greenhouse"], companies=1,
        unchecked=0, all_boards=True, concurrency=2, all_roles=False,
        worldwide=False, include_roles=None, include_qa=False,
        include_management=False, refetch_details=False, include_restricted=False,
        max_results=None, no_cache=True,
    )
    return argparse.Namespace(**{**defaults, **kw})


def test_a_crashing_fetch_records_the_run_as_failed(monkeypatch, _isolated_db) -> None:
    """`finish_run` used to be reachable only on the success path.

    Any exception escaping the source loop therefore left `status='running'`
    forever, which blocks the dashboard from starting another fetch and makes
    the next `mark_stale_runs` clear every live run alongside the dead one.
    Interactively you see the traceback; on a schedule nobody does, and the run
    simply never ends.
    """
    import asyncio

    from jobscrape import cli

    async def boom(*_a, **_kw):
        raise RuntimeError("board fetch exploded")

    monkeypatch.setattr(cli, "_fetch_boards", boom)

    with pytest.raises(RuntimeError, match="exploded"):
        asyncio.run(cli.cmd_fetch(_fetch_args()))

    row = _isolated_db.execute(
        "SELECT status, error, ended_at FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "failed"
    assert row["ended_at"] is not None
    assert "exploded" in (row["error"] or "")


def test_an_interrupted_fetch_also_closes_its_row(monkeypatch, _isolated_db) -> None:
    """Caught as BaseException, not Exception.

    A scheduled run is as likely to be killed — a timeout, a deploy, Ctrl-C —
    as it is to fail, and an interrupt that leaves the row open is the same
    problem wearing a different exception class.

    The recorded `error` text is asyncio's own `CancelledError`, not
    `KeyboardInterrupt` literally — a known asyncio limitation: `gather()`
    wraps each coroutine in a `Task`, and a `Task` cannot cleanly propagate a
    `BaseException` that isn't a plain `Exception` (`KeyboardInterrupt`/
    `SystemExit`) the way a direct, ungathered `await` can; it surfaces as
    `CancelledError` to whatever awaits the `gather()` instead. What matters
    operationally is what this test actually checks: the run still ends up
    `failed`, never stuck at `running` forever, and the interrupt still
    reaches the caller (`pytest.raises` below) so the process still exits
    via it — see `cli.cmd_fetch`'s own comment at the `asyncio.gather` call
    for where this is exercised.
    """
    import asyncio

    from jobscrape import cli

    async def interrupted(*_a, **_kw):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "_fetch_boards", interrupted)

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(cli.cmd_fetch(_fetch_args()))

    row = _isolated_db.execute(
        "SELECT status, error FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "failed"
    assert row["error"]


# --- --exclude-sources: the fetch-fast/fetch-workday split relies on this --

async def test_exclude_sources_drops_the_named_source(monkeypatch, _isolated_db) -> None:
    """`--exclude-sources` is what `runner.STAGES["fetch-fast"]` uses to run
    every source except Workday — confirm it actually removes just the
    named one(s) and leaves the rest selected."""
    from jobscrape import cli

    swept: list[str] = []

    async def fake_fetch_boards(conn, fetcher, ats, since, until, args, boards=None):
        swept.append(ats)
        empty = {"raw": 0, "kept": 0, "new": 0, "refreshed": 0, "boards": 0,
                 "closed": 0, "cooled_down": 0, "quiet": 0}
        return empty, FilterFunnel()

    monkeypatch.setattr(cli, "_fetch_boards", fake_fetch_boards)

    rc = await cli.cmd_fetch(_fetch_args(
        sources=["greenhouse", "lever", "workday"],
        exclude_sources=["workday"],
    ))
    assert rc == 0
    assert set(swept) == {"greenhouse", "lever"}


async def test_exclude_sources_with_no_sources_filter_still_excludes(
    monkeypatch, _isolated_db,
) -> None:
    """`--exclude-sources workday` alone (no `--sources` allowlist) is what
    `fetch-fast` actually runs as — every source except Workday, not just
    every source in some other explicit list."""
    from jobscrape import cli

    swept: list[str] = []

    async def fake_fetch_boards(conn, fetcher, ats, since, until, args, boards=None):
        swept.append(ats)
        empty = {"raw": 0, "kept": 0, "new": 0, "refreshed": 0, "boards": 0,
                 "closed": 0, "cooled_down": 0, "quiet": 0}
        return empty, FilterFunnel()

    async def fake_fetch_one_global_source(name, source, since, until, args, fetcher):
        # `sources=None` also pulls in every keyless global source (hackernews,
        # remoteok, ...) — mock this path too so the test doesn't make real
        # outbound HTTP requests.
        swept.append(name)
        return name, {}, 0

    monkeypatch.setattr(cli, "_fetch_boards", fake_fetch_boards)
    monkeypatch.setattr(cli, "_fetch_one_global_source", fake_fetch_one_global_source)

    rc = await cli.cmd_fetch(_fetch_args(sources=None, exclude_sources=["workday"]))
    assert rc == 0
    assert "workday" not in swept
    assert "greenhouse" in swept  # every other board ATS is still covered


# --- cmd_fetch sweeps ATSes concurrently, each on its own connection --------
#
# `_fetch_one_ats`/`_fetch_one_global_source` (cli.py) replaced a sequential
# `for ats in boards_map:` loop with one `asyncio.gather` task per source, on
# its own DB connection, committing as each one finishes — rather than
# everything sharing one connection and one commit at the very end. These
# tests exercise both properties directly, since neither was true before.

async def test_ats_sweeps_actually_overlap_in_time(monkeypatch, _isolated_db) -> None:
    """The whole point of the change: two ATS sweeps must run concurrently,
    not one after another the way the old sequential loop did. Each fake
    sweep sleeps briefly and records its own start/end; if they were still
    sequential, the second's start would be at or after the first's end."""
    from jobscrape import cli

    intervals: list[tuple[str, float, float]] = []

    async def fake_fetch_boards(conn, fetcher, ats, since, until, args, boards=None):
        start = time.monotonic()
        await asyncio.sleep(0.05)
        intervals.append((ats, start, time.monotonic()))
        empty = {"raw": 0, "kept": 0, "new": 0, "refreshed": 0, "boards": 0,
                 "closed": 0, "cooled_down": 0, "quiet": 0}
        return empty, FilterFunnel()

    monkeypatch.setattr(cli, "_fetch_boards", fake_fetch_boards)

    rc = await cli.cmd_fetch(_fetch_args(sources=["greenhouse", "lever"]))
    assert rc == 0
    assert len(intervals) == 2

    (_, a_start, a_end), (_, b_start, b_end) = intervals
    assert a_start < b_end and b_start < a_end, (
        "ATS sweeps did not overlap in time -- still effectively sequential"
    )


async def test_each_ats_sweep_opens_its_own_connection(monkeypatch, _isolated_db) -> None:
    """`_fetch_one_ats` must call `db.connect()` itself for each ATS, rather
    than reusing one connection shared across every concurrently-running
    sweep — a single Postgres connection cannot safely serve statements from
    multiple coroutines interleaved on the event loop the way sharing one
    across ATSes would require.

    `_isolated_db` (conftest.py) monkeypatches `db.connect` so the whole
    suite shares one rolled-back transaction — real production connections
    would differ per call, but this still proves the CODE PATH asks for one
    per ATS, which is what matters here.
    """
    from jobscrape import cli

    connect_count = 0
    real_connect = db.connect

    def spy_connect(database_url=None):
        nonlocal connect_count
        connect_count += 1
        return real_connect(database_url)

    monkeypatch.setattr(db, "connect", spy_connect)

    # `cmd_fetch` itself also calls `db.connect()` (the outer session) and so
    # do the `dedupe`/`stats` steps it chains afterward -- none of that is
    # what this test cares about. What matters is that each ATS's OWN task
    # sees the counter freshly incremented at the moment it runs, proving a
    # NEW `db.connect()` call happened for it specifically, not that it
    # reused whatever connection an earlier ATS (or the outer session) held.
    per_ats_count: dict[str, int] = {}

    async def fake_fetch_boards(conn, fetcher, ats, since, until, args, boards=None):
        per_ats_count[ats] = connect_count
        empty = {"raw": 0, "kept": 0, "new": 0, "refreshed": 0, "boards": 0,
                 "closed": 0, "cooled_down": 0, "quiet": 0}
        return empty, FilterFunnel()

    monkeypatch.setattr(cli, "_fetch_boards", fake_fetch_boards)

    rc = await cli.cmd_fetch(_fetch_args(sources=["greenhouse", "lever"]))
    assert rc == 0
    assert set(per_ats_count) == {"greenhouse", "lever"}
    assert per_ats_count["greenhouse"] != per_ats_count["lever"], (
        "both ATSes saw the same connect-call count -- they reused one "
        "connection instead of each opening its own"
    )


async def test_a_completed_ats_sweep_commits_before_a_slower_sibling_finishes(
    monkeypatch, _isolated_db,
) -> None:
    """`_fetch_boards` commits per board as it goes (see its own comment in
    cli.py) — confirm a fast ATS's insert calls `commit()` on its own
    connection while a slower sibling ATS is still mid-sweep, rather than
    everything waiting for the whole `cmd_fetch` call to finish the way a
    single shared, once-at-the-end transaction would."""
    from jobscrape import cli

    commits_while_sibling_running: list[bool] = []
    lever_started = asyncio.Event()
    lever_may_finish = asyncio.Event()

    async def fake_fetch_boards(conn, fetcher, ats, since, until, args, boards=None):
        empty = {"raw": 0, "kept": 0, "new": 0, "refreshed": 0, "boards": 0,
                 "closed": 0, "cooled_down": 0, "quiet": 0}
        if ats == "lever":
            lever_started.set()
            await lever_may_finish.wait()
            return empty, FilterFunnel()

        # greenhouse: wait for lever to genuinely be mid-flight, then insert
        # and commit while it's still running.
        await lever_started.wait()
        db.upsert_jobs(conn, [Job(
            source="greenhouse", source_job_id="early1", title="Software Engineer",
            company="Acme", company_slug="acme", job_url="https://x/early1",
        )])
        conn.commit()
        commits_while_sibling_running.append(True)
        lever_may_finish.set()
        return empty, FilterFunnel()

    monkeypatch.setattr(cli, "_fetch_boards", fake_fetch_boards)

    rc = await cli.cmd_fetch(_fetch_args(sources=["greenhouse", "lever"]))
    assert rc == 0
    assert commits_while_sibling_running == [True]


# --- connecting must not take locks a running sweep is holding ---------------


def test_no_ddl_runs_when_the_schema_is_current(_isolated_db, monkeypatch):
    """`db.connect()` used to run the whole schema DDL every single time.

    `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` are no-ops
    when the object exists, but they are not free of LOCKS — `CREATE INDEX`
    takes a SHARE lock on the table just to discover it has nothing to do, and
    SHARE conflicts with the ROW EXCLUSIVE a writer holds.

    Observed for real: opening the dashboard during a fetch deadlocked the two
    against each other, and Postgres resolved it by killing the fetch six ATSes
    in —

        Process A waits for RowExclusiveLock on relation `jobs`, blocked by B.
        Process B waits for ShareLock on relation `companies`, blocked by A.

    Any second process connecting mid-sweep could do it: a `jobscrape stats`, a
    dashboard restart, the webhook receiver. That stops being rare once runs
    are on cron, which is why the DDL is now conditional.
    """
    from jobscrape import db

    ran: list[str] = []
    monkeypatch.setattr(db, "_apply_indexes", lambda conn: ran.append("indexes"))
    monkeypatch.setattr(db, "_migrate", lambda conn: ran.append("migrate"))
    monkeypatch.setattr(db, "_SCHEMA_READY", set())

    assert db._schema_is_current(_isolated_db) is True
    assert db.ensure_schema(_isolated_db, "test-url") is False
    assert ran == [], "connect ran schema DDL against an already-current database"


def test_the_schema_is_still_applied_when_something_is_missing(
    _isolated_db, monkeypatch
):
    """The other half, and the one that matters for a fresh install or a
    release that adds a column: the check must not be a blanket True."""
    from jobscrape import db

    ran: list[str] = []
    monkeypatch.setattr(db, "_apply_indexes", lambda conn: ran.append("indexes"))
    monkeypatch.setattr(db, "_migrate", lambda conn: ran.append("migrate"))
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    # A column the live table does not have — the shape of a schema change.
    monkeypatch.setattr(db, "JOB_COLUMNS", [*db.JOB_COLUMNS, "a_column_added_next_release"])

    assert db._schema_is_current(_isolated_db) is False
    assert db.ensure_schema(_isolated_db, "test-url") is True
    assert ran == ["migrate", "indexes"]


def test_a_missing_companies_or_schedules_column_trips_the_ddl_too(
    _isolated_db, monkeypatch
):
    """`companies`/`schedules` used to be existence-checked only, not
    column-diffed the way `jobs` already was — a real gap, since
    `docs/DOCKER.md`'s documented Update flow (`docker compose build && up
    -d`) never runs `alembic upgrade head`; only the one-time fresh-install
    path does. A column added to either table after its `CREATE TABLE`
    first shipped would silently never reach an already-running
    deployment."""
    from jobscrape import db

    ran: list[str] = []
    monkeypatch.setattr(db, "_apply_indexes", lambda conn: ran.append("indexes"))
    monkeypatch.setattr(db, "_migrate", lambda conn: ran.append("migrate"))
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    monkeypatch.setattr(
        db, "_COMPANY_MIGRATED_COLUMNS",
        {*db._COMPANY_MIGRATED_COLUMNS, "a_column_added_next_release"},
    )

    assert db._schema_is_current(_isolated_db) is False
    assert db.ensure_schema(_isolated_db, "test-url") is True
    assert ran == ["migrate", "indexes"]


def test_migrate_actually_restores_a_dropped_companies_column(_isolated_db) -> None:
    """Not just that the check trips — the self-heal DDL must actually put
    the column back, the same guarantee `jobs` already had."""
    from jobscrape import db

    _isolated_db.execute("ALTER TABLE companies DROP COLUMN quiet_until")
    db._migrate(_isolated_db)
    cols = {
        r["column_name"] for r in _isolated_db.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'companies'"
        )
    }
    assert "quiet_until" in cols


def test_the_expected_index_count_tracks_the_script() -> None:
    """Hard-coding it would silently skip the DDL for everyone the day an index
    is added, so it is counted from `INDEXES` itself."""
    from jobscrape import db

    assert db._EXPECTED_INDEX_COUNT == db.INDEXES.upper().count("CREATE INDEX")
