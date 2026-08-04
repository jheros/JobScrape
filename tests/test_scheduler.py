"""The in-process scheduler: reload from `schedules`, fire through RunManager.

Never calls `scheduler.start()`/`shutdown()` here — that would start the real
background job runner for a module-level singleton shared across the whole
suite. `reload()` and `_fire()` are tested directly, which is where the actual
logic lives; APScheduler firing a `CronTrigger` on time is its own library's
concern, not this project's.
"""

from __future__ import annotations

import pytest

from jobscrape import db
from jobscrape.dashboard import scheduler
from jobscrape.dashboard.runner import manager


@pytest.fixture(autouse=True)
def _clear_jobs():
    yield
    scheduler.scheduler.remove_all_jobs()


def test_reload_adds_a_job_only_for_enabled_schedules(_isolated_db) -> None:
    conn = _isolated_db
    enabled_id = db.create_schedule(conn, "Daily", "fetch", enabled=True)
    db.create_schedule(conn, "Disabled", "fetch", enabled=False)

    scheduler.reload()

    job_ids = {job.id for job in scheduler.scheduler.get_jobs()}
    assert job_ids == {scheduler._job_id(enabled_id)}


def test_reload_is_idempotent(_isolated_db) -> None:
    conn = _isolated_db
    db.create_schedule(conn, "Daily", "fetch")
    scheduler.reload()
    scheduler.reload()
    assert len(scheduler.scheduler.get_jobs()) == 1


async def test_fire_starts_the_stage_and_records_the_run(_isolated_db, monkeypatch) -> None:
    conn = _isolated_db
    schedule_id = db.create_schedule(conn, "Daily", "stats", extra="")

    async def fake_start(stage, extra=None, window=None):
        assert stage == "stats"
        return {"run_id": 999, "stage": stage, "pid": 1}

    monkeypatch.setattr(manager, "start", fake_start)
    await scheduler._fire(schedule_id)

    row = db.get_schedule(conn, schedule_id)
    assert row["last_run_id"] == 999
    assert row["last_fired_at"] is not None


async def test_fire_skips_a_disabled_schedule(_isolated_db, monkeypatch) -> None:
    conn = _isolated_db
    schedule_id = db.create_schedule(conn, "Off", "stats", enabled=False)

    called = False

    async def fake_start(stage, extra=None, window=None):
        nonlocal called
        called = True
        return {"run_id": 1, "stage": stage, "pid": 1}

    monkeypatch.setattr(manager, "start", fake_start)
    await scheduler._fire(schedule_id)
    assert called is False


async def test_fire_swallows_a_busy_stage_conflict(_isolated_db, monkeypatch) -> None:
    """A RunManager RuntimeError (stage/group already busy) is an expected,
    benign collision — e.g. daily and weekly landing close together — not
    something that should raise into APScheduler's executor."""
    conn = _isolated_db
    schedule_id = db.create_schedule(conn, "Daily", "fetch")

    async def fake_start(stage, extra=None, window=None):
        raise RuntimeError(f"{stage} is already running")

    monkeypatch.setattr(manager, "start", fake_start)
    await scheduler._fire(schedule_id)  # must not raise

    row = db.get_schedule(conn, schedule_id)
    assert row["last_run_id"] is None


async def test_fire_forwards_extra_flags(_isolated_db, monkeypatch) -> None:
    conn = _isolated_db
    schedule_id = db.create_schedule(conn, "Recheck", "validate", extra="--recheck")

    seen = {}

    async def fake_start(stage, extra=None, window=None):
        seen["stage"] = stage
        seen["extra"] = extra
        return {"run_id": 1, "stage": stage, "pid": 1}

    monkeypatch.setattr(manager, "start", fake_start)
    await scheduler._fire(schedule_id)
    assert seen == {"stage": "validate", "extra": ["--recheck"]}


async def test_fire_resolves_a_relative_window_preset(_isolated_db, monkeypatch) -> None:
    """A schedule's `stage` may be a `RELATIVE_WINDOWS` preset name (e.g.
    "fetch-1w") rather than a real `STAGES` key — `_fire()` must resolve it
    to the real base stage plus a `since` computed fresh on this fire, never
    a date fixed back when the schedule was created."""
    from datetime import date, timedelta

    conn = _isolated_db
    schedule_id = db.create_schedule(conn, "Weekly fetch", "fetch-1w", extra="")

    seen = {}

    async def fake_start(stage, extra=None, window=None):
        seen["stage"] = stage
        seen["window"] = window
        return {"run_id": 1, "stage": stage, "pid": 1}

    monkeypatch.setattr(manager, "start", fake_start)
    await scheduler._fire(schedule_id)

    assert seen["stage"] == "fetch"
    since, until = seen["window"]
    assert since == date.today() - timedelta(days=7)
    assert until is None


async def test_fire_passes_no_window_for_a_plain_stage(_isolated_db, monkeypatch) -> None:
    """`full-sweep` (and any other real `STAGES` key) isn't in
    `RELATIVE_WINDOWS`, so it must fire exactly as before — no window."""
    conn = _isolated_db
    schedule_id = db.create_schedule(conn, "Full", "full-sweep", extra="")

    seen = {}

    async def fake_start(stage, extra=None, window=None):
        seen["stage"] = stage
        seen["window"] = window
        return {"run_id": 1, "stage": stage, "pid": 1}

    monkeypatch.setattr(manager, "start", fake_start)
    await scheduler._fire(schedule_id)

    assert seen == {"stage": "full-sweep", "window": None}
