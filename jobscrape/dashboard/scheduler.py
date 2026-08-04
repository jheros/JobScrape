"""The dashboard's own scheduler — replaces crontab as the source of truth.

`schedules` (see `db.py`) is the single source of configuration; APScheduler's
own job-store persistence is deliberately not used, so a schedule stays a
plain, inspectable/editable row rather than something serialized into
APScheduler's internal format. On `start()` — and again on every `reload()`,
which every create/update/delete/toggle route calls — every `enabled` row is
read fresh and added as a job; nothing about a schedule survives in memory
across a `reload()` except what's back in that row.

`AsyncIOScheduler`, not the thread-based `BackgroundScheduler`: a job callback
calls `await manager.start(...)`, which uses `asyncio.Lock` and
`asyncio.create_subprocess_exec` on the SAME event loop uvicorn is already
running. A thread-based scheduler would call into that from a different
thread, which is not where those primitives are safe to touch.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .. import config, db
from .runner import manager, resolve_stage

log = logging.getLogger("jobscrape.dashboard.scheduler")

scheduler = AsyncIOScheduler()


def _job_id(schedule_id: int) -> str:
    return f"schedule-{schedule_id}"


async def _fire(schedule_id: int) -> None:
    """A schedule's cron trigger fired. Reload the row (so an edit made after
    `reload()` added this job is honoured without live-mutating a trigger),
    then start the stage through the same `RunManager` every manual click
    goes through.

    A schedule's `stage` may be one of `RELATIVE_WINDOWS`'s preset names
    (e.g. "fetch-1w") instead of a real `STAGES` key — `resolve_stage()`
    turns it into the real stage plus a `since` computed fresh on *this*
    fire, never a date fixed back when the schedule was created.
    """
    with db.session() as conn:
        row = db.get_schedule(conn, schedule_id)
    if not row or not row["enabled"]:
        return  # disabled/deleted since this fire was scheduled
    extra = row["extra"].split() if row["extra"] else None
    stage, window = resolve_stage(row["stage"])

    try:
        info = await manager.start(stage, extra, window=window)
    except (ValueError, RuntimeError) as exc:
        # Expected and benign — e.g. the daily and weekly schedules landing
        # close together, or a manual run already occupying this stage. Not
        # an error worth an APScheduler executor traceback.
        log.info("schedule %s (%s) skipped: %s", schedule_id, stage, exc)
        return
    with db.session() as conn:
        db.record_schedule_fired(conn, schedule_id, info["run_id"])
    log.info("schedule %s fired %s as run %s", schedule_id, stage, info["run_id"])
    maybe_sync_after(stage)


def maybe_sync_after(stage: str) -> None:
    """Fire-and-forget: push what `stage` just wrote to Render once it
    finishes, if this database is configured to sync at all.

    Shared by the cron fire path above and every manual trigger in `app.py`
    (a Runs-page button, Schedule tab's "Run now") — a manual click should
    reach Render exactly the same as a schedule firing, not require its own
    separate button.
    """
    if stage != "render-sync" and config.RENDER_DATABASE_URL:
        # Fire-and-forget: don't hold up the caller waiting on this.
        asyncio.create_task(_sync_after(stage))


async def _sync_after(stage: str) -> None:
    """Wait for `stage` to stop running, then push what it wrote to Render.

    Polls `manager.is_running` rather than watching the run row directly —
    that's the same state `RunManager` already tracks, so no new bookkeeping
    is needed to know when a run is done.
    """
    while manager.is_running(stage):
        await asyncio.sleep(15)
    try:
        await manager.start("render-sync")
    except (ValueError, RuntimeError) as exc:
        # Benign — e.g. a manually-started render-sync already running.
        log.info("render-sync after %s skipped: %s", stage, exc)


def reload() -> None:
    """Drop every job and re-add one per enabled row. Called at startup and
    after any create/update/delete/toggle so an edit takes effect immediately,
    with no dashboard restart needed."""
    scheduler.remove_all_jobs()
    with db.session() as conn:
        rows = db.list_schedules(conn)
    for row in rows:
        if not row["enabled"]:
            continue
        scheduler.add_job(
            _fire,
            trigger=CronTrigger(
                minute=row["cron_minute"], hour=row["cron_hour"],
                day_of_week=row["cron_day_of_week"], timezone=row["timezone"],
            ),
            args=[row["id"]],
            id=_job_id(row["id"]),
            replace_existing=True,
            misfire_grace_time=3600,
        )
    log.info("scheduler reloaded: %d active job(s)", len(scheduler.get_jobs()))


def start() -> None:
    reload()
    scheduler.start()


def shutdown() -> None:
    scheduler.shutdown(wait=False)
