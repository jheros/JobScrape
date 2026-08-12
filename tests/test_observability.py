"""Structured logging and health checks."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from jobscrape import db
from jobscrape.observability import JsonFormatter, health


def _record(**extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="jobscrape.test", level=logging.INFO, pathname=__file__,
        lineno=1, msg="hello %s", args=("world",), exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_valid_json_with_the_message_rendered() -> None:
    line = JsonFormatter().format(_record())
    payload = json.loads(line)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "jobscrape.test"
    assert "ts" in payload


def test_json_formatter_carries_extra_fields() -> None:
    payload = json.loads(JsonFormatter().format(_record(run_id=42, stage="fetch")))
    assert payload["run_id"] == 42
    assert payload["stage"] == "fetch"


def test_json_formatter_renders_exceptions() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="t", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=(), exc_info=__import__("sys").exc_info(),
        )
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exc_info"]


def test_health_reports_a_reachable_database() -> None:
    result = health()
    assert result["healthy"] is True
    assert result["checks"]["database"]["ok"] is True
    assert result["checks"]["disk"]["ok"] is True


def test_health_unreachable_database_is_unhealthy(monkeypatch) -> None:
    """Simulate the outage directly at `db.session` — the conftest fixture
    already owns `db.connect` for test isolation, so this patches a different
    attribute rather than fighting it."""
    import jobscrape.observability as obs

    class _Boom:
        def __enter__(self):
            raise ConnectionError("simulated outage")

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(obs.db, "session", lambda *a, **kw: _Boom())
    result = obs.health()
    assert result["healthy"] is False
    assert result["checks"]["database"]["ok"] is False


# --- stuck stages: the real 2026-08-12 outage this check exists for --------
#
# render-sync and jobright-fetch failed on every attempt for 13+ hours while
# /health stayed green, because `last_successful_run` alone only ever looks
# at the single most recent OK run ACROSS EVERY STAGE — `fetch` itself kept
# succeeding the whole time, since it doesn't touch the network path that
# broke. These tests are the regression guard for that blind spot.


def _run(conn, stage: str, status: str, ended_at=None):
    run_id = db.start_run(conn, "test", stage)
    db.finish_run(conn, run_id, status=status, error="boom" if status == "failed" else None)
    if ended_at is not None:
        conn.execute("UPDATE runs SET ended_at = ? WHERE id = ?", (ended_at, run_id))
    return run_id


def test_health_flags_a_stage_that_failed_its_last_three_attempts() -> None:
    with db.session() as conn:
        for _ in range(3):
            _run(conn, "render-sync", "failed")
        # An unrelated stage kept succeeding the whole time -- must not mask
        # the stuck one, the exact blind spot `last_successful_run` had.
        _run(conn, "fetch", "ok")

    result = health()
    assert result["healthy"] is False
    stuck = {s["stage"] for s in result["checks"]["stuck_stages"]}
    assert stuck == {"render-sync"}


def test_health_does_not_flag_a_stage_with_a_recent_success() -> None:
    with db.session() as conn:
        _run(conn, "render-sync", "failed")
        _run(conn, "render-sync", "ok")
        _run(conn, "render-sync", "failed")

    result = health()
    assert result["checks"]["stuck_stages"] == []
    assert result["healthy"] is True


def test_health_does_not_flag_a_stage_with_too_few_runs() -> None:
    """Two failures is not yet three -- must not fire on the first blip."""
    with db.session() as conn:
        _run(conn, "render-sync", "failed")
        _run(conn, "render-sync", "failed")

    result = health()
    assert result["checks"]["stuck_stages"] == []
    assert result["healthy"] is True


def test_health_ignores_stale_failures_outside_the_lookback_window() -> None:
    """A stage retired from the schedule long ago, whose last three runs
    (whenever they were) all happened to fail, must not flag forever."""
    stale = datetime.now(timezone.utc) - timedelta(days=30)
    with db.session() as conn:
        for _ in range(3):
            _run(conn, "old-stage", "failed", ended_at=stale)

    result = health()
    assert result["checks"]["stuck_stages"] == []
    assert result["healthy"] is True
