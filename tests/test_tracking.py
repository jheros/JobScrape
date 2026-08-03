"""Closure detection, run history, and your triage decisions.

Two failure modes here are severe and silent:

* Inferring closures from a *failed* board fetch would wipe a company's whole
  listing because of one network error.
* Letting a re-fetch overwrite job_status would erase the record of where you
  applied.

Both are guarded explicitly below.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jobscrape import config, db
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


def test_failed_fetch_closes_nothing(tmp_path) -> None:
    """The guard that matters most.

    A board that errors yields no ids. Treating that as "everything closed"
    would destroy the listing on any transient network fault.
    """
    with db.session(tmp_path / "c.db") as conn:
        db.upsert_jobs(conn, [_job("a"), _job("b")])
        _backdate_last_seen(conn, "a", config.CLOSURE_GRACE_HOURS + 1)
        _backdate_last_seen(conn, "b", config.CLOSURE_GRACE_HOURS + 1)
        assert db.mark_board_closures(conn, "ashby", "acme", set()) == 0
        still_open = conn.execute(
            "SELECT COUNT(*) c FROM jobs WHERE closed_at IS NULL"
        ).fetchone()["c"]
        assert still_open == 2


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
    assert "KeyboardInterrupt" in (row["error"] or "")


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


def test_the_expected_index_count_tracks_the_script() -> None:
    """Hard-coding it would silently skip the DDL for everyone the day an index
    is added, so it is counted from `INDEXES` itself."""
    from jobscrape import db

    assert db._EXPECTED_INDEX_COUNT == db.INDEXES.upper().count("CREATE INDEX")
