"""Structured logging and health checks.

Kept separate from `config.py` (settings) and `db.py` (storage) since this
module is about *watching* the running system, not the system itself.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, db

#: Attributes every LogRecord carries regardless of what was logged — used to
#: separate "the message" from anything a caller attached via `extra=`.
_STANDARD_RECORD_KEYS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line — easy to ship to a log aggregator.

    Deliberately minimal: timestamp, level, logger, message, plus anything
    passed via ``extra=`` (e.g. ``run_id``, ``stage``) riding along without
    needing a fixed schema up front. Exceptions render as a string under
    ``exc_info`` so a JSON-consuming pipeline still gets a full traceback.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_KEYS and key not in payload:
                payload[key] = value
        return json.dumps(payload, default=str)


def setup_logging(verbose: bool = False, fmt: str | None = None) -> None:
    """Configure the root logger once, for either humans or a log pipeline.

    ``fmt`` defaults to ``config.LOG_FORMAT`` (``$JOBSCRAPE_LOG_FORMAT``) so
    the choice is one environment variable, not a flag every entry point has
    to remember to pass through.
    """
    fmt = fmt or config.LOG_FORMAT
    level = logging.DEBUG if verbose else getattr(logging, config.LOG_LEVEL, logging.INFO)

    handler = logging.StreamHandler()
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # httpcore/httpx DEBUG output is per-packet and drowns everything else.
    for noisy in ("httpx", "httpcore", "hpack", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


#: How many consecutive failures of the SAME stage counts as "stuck", not
#: just one unlucky run. At `render-sync`'s/`jobright-fetch`'s own ~20-30
#: minute cadence this catches a genuine outage within roughly an hour,
#: instead of however long it takes someone to notice by hand.
_STUCK_STAGE_THRESHOLD = 3

#: How far back a stage's run history is even considered — without this, a
#: stage retired from the schedule long ago (its last 3 runs, whenever they
#: were, all happened to fail) would flag forever with nothing to fix.
_STUCK_STAGE_LOOKBACK = timedelta(days=2)


def _stuck_stages(conn: db.Connection) -> list[dict[str, Any]]:
    """Stages whose most recent `_STUCK_STAGE_THRESHOLD` runs are ALL
    'failed'.

    The blind spot `last_successful_run` alone has: that check only ever
    looks at the single most-recent OK run across EVERY stage, so one
    healthy stage (`fetch`, say) hides another that has been failing on
    every single attempt (`render-sync`, `jobright-fetch`) for hours —
    exactly what happened for real on 2026-08-12: a host-level network
    outage silently broke render-sync and jobright-fetch for 13+ hours
    while `/health` stayed green the entire time, because `fetch` itself
    doesn't touch the network path that broke.
    """
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT stage, status, ended_at,
                   ROW_NUMBER() OVER (PARTITION BY stage ORDER BY id DESC) AS rn
            FROM runs
            WHERE stage IS NOT NULL AND ended_at > ?
        )
        SELECT stage, MAX(ended_at) AS last_failed_at
        FROM ranked
        WHERE rn <= ?
        GROUP BY stage
        HAVING COUNT(*) = ? AND COUNT(*) FILTER (WHERE status = 'failed') = ?
        """,
        (
            (datetime.now(timezone.utc) - _STUCK_STAGE_LOOKBACK).isoformat(),
            _STUCK_STAGE_THRESHOLD, _STUCK_STAGE_THRESHOLD, _STUCK_STAGE_THRESHOLD,
        ),
    ).fetchall()
    return [
        {"stage": r["stage"], "last_failed_at": r["last_failed_at"].isoformat()}
        for r in rows
    ]


def health() -> dict[str, Any]:
    """Real dependency checks, not just "the process is alive".

    Returns ``{"healthy": bool, "checks": {...}}`` so `/health` can return the
    right HTTP status and a body worth reading, not just a 200 that means
    nothing more than "the web server thread is scheduled."
    """
    checks: dict[str, Any] = {}
    healthy = True
    last_run = None
    stuck_stages: list[dict[str, Any]] = []

    start = time.monotonic()
    try:
        with db.session() as conn:
            conn.execute("SELECT 1")
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            pool = db._get_engine(config.DATABASE_URL).pool
            checks["database"] = {
                "ok": True,
                "latency_ms": latency_ms,
                "pool_checked_out": pool.checkedout(),
                "pool_size": pool.size(),
            }
            last_run = conn.execute(
                "SELECT ended_at FROM runs WHERE status = 'ok' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            stuck_stages = _stuck_stages(conn)
    except Exception as exc:
        healthy = False
        checks["database"] = {"ok": False, "error": str(exc)}

    checks["stuck_stages"] = stuck_stages
    if stuck_stages:
        healthy = False

    if last_run and last_run["ended_at"]:
        ended_at = last_run["ended_at"]
        age_hours = (datetime.now(timezone.utc) - ended_at).total_seconds() / 3600
        checks["last_successful_run"] = {
            "ended_at": ended_at.isoformat(), "age_hours": round(age_hours, 1),
        }
    else:
        checks["last_successful_run"] = None

    try:
        usage = shutil.disk_usage(config.DATA_DIR)
        free_pct = round(100 * usage.free / usage.total, 1)
        checks["disk"] = {"ok": free_pct > 5, "free_pct": free_pct}
        if free_pct <= 5:
            healthy = False
    except OSError as exc:
        checks["disk"] = {"ok": False, "error": str(exc)}

    return {"healthy": healthy, "checks": checks}
