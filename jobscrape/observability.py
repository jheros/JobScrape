"""Structured logging and health checks.

Kept separate from `config.py` (settings) and `db.py` (storage) since this
module is about *watching* the running system, not the system itself.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timezone
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


def health() -> dict[str, Any]:
    """Real dependency checks, not just "the process is alive".

    Returns ``{"healthy": bool, "checks": {...}}`` so `/health` can return the
    right HTTP status and a body worth reading, not just a 200 that means
    nothing more than "the web server thread is scheduled."
    """
    checks: dict[str, Any] = {}
    healthy = True
    last_run = None

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
    except Exception as exc:
        healthy = False
        checks["database"] = {"ok": False, "error": str(exc)}

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
