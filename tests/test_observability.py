"""Structured logging and health checks."""

from __future__ import annotations

import json
import logging

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
