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


def test_reload_skips_one_invalid_trigger_without_dropping_the_rest(_isolated_db) -> None:
    """One row's own cron/timezone being invalid must not take every other
    schedule down with it. `remove_all_jobs()` runs unconditionally first,
    so a raise partway through the add loop used to leave the scheduler with
    NO active jobs at all, valid schedules included -- and since every
    create/update/toggle route calls `reload()` synchronously, it would also
    fail whatever unrelated edit triggered this reload."""
    conn = _isolated_db
    good_id = db.create_schedule(conn, "Daily", "fetch")
    db.create_schedule(conn, "Bad timezone", "stats", timezone="Not/A_Real_Zone")

    scheduler.reload()  # must not raise

    job_ids = {job.id for job in scheduler.scheduler.get_jobs()}
    assert job_ids == {scheduler._job_id(good_id)}


def test_reload_builds_interval_trigger_for_a_days_schedule(_isolated_db) -> None:
    """`interval_days` set means "every N days", via APScheduler's
    IntervalTrigger rather than CronTrigger -- cron has no native rolling
    N-day concept."""
    from apscheduler.triggers.interval import IntervalTrigger

    conn = _isolated_db
    schedule_id = db.create_schedule(
        conn, "Every 2 days", "discover-validate", cron_hour="7", cron_minute="30",
        interval_days=2,
    )
    scheduler.reload()

    job = scheduler.scheduler.get_job(scheduler._job_id(schedule_id))
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval.days == 2


def test_reload_anchors_the_interval_on_created_at_not_now(_isolated_db) -> None:
    """`reload()` fires on every schedule create/update/toggle/delete, not
    just this row's own -- an anchor recomputed from "now" each time would
    silently reset the N-day counter whenever an unrelated schedule was
    touched. `created_at` is immutable, so it has to be the anchor."""
    from apscheduler.triggers.interval import IntervalTrigger

    conn = _isolated_db
    schedule_id = db.create_schedule(
        conn, "Every 3 days", "discover-validate", cron_hour="9", cron_minute="15",
        interval_days=3,
    )
    created_at = db.get_schedule(conn, schedule_id)["created_at"]
    scheduler.reload()

    job = scheduler.scheduler.get_job(scheduler._job_id(schedule_id))
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.start_date.date() == created_at.date()
    assert job.trigger.start_date.hour == 9
    assert job.trigger.start_date.minute == 15


def test_reload_handles_a_mix_of_cron_and_interval_schedules(_isolated_db) -> None:
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    conn = _isolated_db
    cron_id = db.create_schedule(conn, "Daily", "fetch")
    interval_id = db.create_schedule(
        conn, "Every 2 days", "discover-validate", interval_days=2,
    )
    scheduler.reload()

    cron_job = scheduler.scheduler.get_job(scheduler._job_id(cron_id))
    interval_job = scheduler.scheduler.get_job(scheduler._job_id(interval_id))
    assert isinstance(cron_job.trigger, CronTrigger)
    assert isinstance(interval_job.trigger, IntervalTrigger)


def test_reload_skips_an_invalid_interval_without_dropping_the_rest(_isolated_db) -> None:
    """An out-of-range hour/minute on an interval schedule raises building
    the anchor `datetime.combine(...)` -- same "one bad row must not take
    the rest down with it" contract `reload()` already has for a bad cron
    field/timezone."""
    conn = _isolated_db
    good_id = db.create_schedule(conn, "Daily", "fetch")
    db.create_schedule(
        conn, "Bad hour", "discover-validate", cron_hour="99", interval_days=2,
    )

    scheduler.reload()  # must not raise

    job_ids = {job.id for job in scheduler.scheduler.get_jobs()}
    assert job_ids == {scheduler._job_id(good_id)}


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


async def test_fire_checks_render_sync_after_the_stage(_isolated_db, monkeypatch) -> None:
    """A cron fire must reach the same `maybe_sync_after` check a manual
    Runs-page click or "Run now" goes through (see test_dashboard_routes.py)
    — same helper, so all three trigger paths behave identically."""
    conn = _isolated_db
    schedule_id = db.create_schedule(conn, "Daily", "apify-pull", extra="")

    async def fake_start(stage, extra=None, window=None):
        return {"run_id": 1, "stage": stage, "pid": 1}

    seen = []
    monkeypatch.setattr(manager, "start", fake_start)
    monkeypatch.setattr(scheduler, "maybe_sync_after", seen.append)
    await scheduler._fire(schedule_id)

    assert seen == ["apify-pull"]


async def test_maybe_sync_after_task_is_referenced_until_done(monkeypatch) -> None:
    """`asyncio`'s event loop holds only a WEAK reference to a task returned
    by `create_task()` -- with nothing else referencing it, the task is
    eligible for garbage collection at any point during its
    `await asyncio.sleep(15)` polling loop, silently cancelling the pending
    render-sync with no error logged anywhere. `_pending_syncs` must hold a
    strong reference until the task's own done-callback removes it."""
    from jobscrape import config as cfg

    monkeypatch.setattr(cfg, "RENDER_DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setattr(manager, "is_running", lambda stage: False)

    started = []

    async def fake_start(stage, **kw):
        started.append(stage)
        return {"run_id": 1, "stage": stage, "pid": 1}

    monkeypatch.setattr(manager, "start", fake_start)

    scheduler.maybe_sync_after("fetch")
    assert len(scheduler._pending_syncs) == 1

    task = next(iter(scheduler._pending_syncs))
    await task

    assert started == ["render-sync"]
    assert scheduler._pending_syncs == set()
