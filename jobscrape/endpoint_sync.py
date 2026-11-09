"""Push collected jobs to external HTTP endpoints managed from the dashboard.

An output target, like `render_sync`, but over HTTP to any number of
receivers (`sync_endpoints` rows) instead of one database. Each endpoint is
synced independently from its OWN cursor — the (changed_at, source,
source_job_id) of the last row it accepted — so a receiver that is down or
rejecting the token never holds the others back, and catches up by itself
once it recovers.

Wire format, one request per batch:

    POST <url>
    Authorization: Bearer <token>
    Content-Type: application/json

    {"rows": [{<ROW_COLUMNS>}, ...]}

and, once per run when anything changed on the receiver's side:

    {"rows": [], "announce": {"inserted": n, "updated": n, "reopened": n, "closed": n}}

The receiver answers 2xx with
``{"inserted", "updated", "reopened", "closed", "skipped", "rejected": [...]}``
(missing keys read as 0).
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Callable

import httpx

from . import db

log = logging.getLogger("jobscrape.endpoint_sync")

#: Exactly the keys of each row sent, JobScrape's own column names.
ROW_COLUMNS = (
    "source", "source_job_id", "title", "company", "job_url", "apply_url",
    "description_text", "workplace_type", "ai_workplace_type", "city", "state",
    "country", "location_raw", "seniority", "ai_seniority", "salary_min",
    "salary_max", "salary_currency", "employment_type", "skills", "department",
    "industry", "ai_requires_clearance", "posted_at", "closed_at", "duplicate_of",
    "scam_signals",
)

#: Sources whose postings must never leave this system.
RESTRICTED_SOURCES = ("linkedin", "indeed", "avature")

_PAGE_SIZE = 500
_MAX_ROWS_PER_REQUEST = 500
_MAX_BODY_BYTES = 4 * 1024 * 1024
_TIMEOUT_SECONDS = 120.0
_TEST_TIMEOUT_SECONDS = 20.0
_RETRY_DELAYS = (2, 8, 30)
_MAX_REJECTED_LOGGED = 20
_COUNT_KEYS = ("inserted", "updated", "reopened", "closed", "skipped")
_ANNOUNCE_KEYS = ("inserted", "updated", "reopened", "closed")

#: Same "changed" definition as `render_sync`: a closure or a dedupe
#: suppression updates `closed_at`/`duplicate_marked_at` WITHOUT touching
#: `last_seen_at`, so all three must be watched.
_CHANGED_AT = (
    "GREATEST(last_seen_at, COALESCE(closed_at, last_seen_at), "
    "COALESCE(duplicate_marked_at, last_seen_at))"
)

#: This module's own slot in Postgres's advisory-lock keyspace — distinct
#: from `render_sync._LOCK_KEY`, since the two never touch the same state.
_LOCK_KEY = 913_427_730_271


class EndpointSyncLocked(Exception):
    """Another `sync()` already holds the lock."""


class _EndpointFailed(Exception):
    """Stops one endpoint's pass; the message is safe to store and log."""


@contextmanager
def _locked(conn: db.Connection):
    """Session-scoped, non-blocking — the same contract as
    `render_sync._locked`: a second caller (an overlapping schedule, a cron
    line plus a dashboard click) skips cleanly instead of queuing."""
    got = conn.execute("SELECT pg_try_advisory_lock(?) AS ok", (_LOCK_KEY,)).fetchone()["ok"]
    if not got:
        raise EndpointSyncLocked("another endpoint-sync is already running")
    try:
        yield
    finally:
        conn.execute("SELECT pg_advisory_unlock(?)", (_LOCK_KEY,))


def _make_client() -> httpx.Client:
    return httpx.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=False)


def _select_sql(first_sync: bool, after_cursor: bool) -> str:
    restricted = ", ".join(f"'{s}'" for s in RESTRICTED_SOURCES)
    where = [f"source NOT IN ({restricted})"]
    if first_sync:
        # A receiver that never had these has nothing to close.
        where.append("closed_at IS NULL AND duplicate_of IS NULL")
    if after_cursor:
        # Strictly after, over all three columns: one `upsert_jobs` call stamps
        # one `last_seen_at` on every row it touches, so ties on changed_at
        # alone are the common case.
        where.append(
            f"({_CHANGED_AT}, source, source_job_id) > (CAST(? AS timestamptz), ?, ?)"
        )
    # `::text` keeps the microseconds exactly, for the cursor round-trip.
    return (
        f"SELECT {', '.join(ROW_COLUMNS)}, {_CHANGED_AT}::text AS changed_at_text "
        f"FROM jobs WHERE {' AND '.join(where)} "
        f"ORDER BY {_CHANGED_AT}, source, source_job_id LIMIT ?"
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _to_row(job_row: dict[str, Any]) -> dict[str, Any]:
    return {col: _jsonable(job_row.get(col)) for col in ROW_COLUMNS}


def _body(serialized_rows: list[str]) -> bytes:
    return ('{"rows": [' + ", ".join(serialized_rows) + "]}").encode()


def _redact(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def _post(
    client: httpx.Client, url: str, token: str, body: bytes,
    sleep: Callable[[float], Any], timeout: float = _TIMEOUT_SECONDS,
    retry: bool = True,
) -> httpx.Response:
    """POST with retries on network errors, 429 and 5xx; any other non-2xx —
    a 3xx included, since redirects are never followed — raises at once."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    delays = _RETRY_DELAYS if retry else ()
    error = ""
    for attempt in range(len(delays) + 1):
        try:
            response = client.post(
                url, content=body, headers=headers, timeout=timeout,
                follow_redirects=False,
            )
        except httpx.TransportError as exc:
            error = f"network error: {type(exc).__name__}: {exc}"
        else:
            status = response.status_code
            if 200 <= status < 300:
                return response
            snippet = response.text[:200].strip()
            error = f"HTTP {status}" + (f": {snippet}" if snippet else "")
            if status != 429 and status < 500:
                raise _EndpointFailed(_redact(error, token))
        if attempt < len(delays):
            sleep(delays[attempt])
    if delays:
        error += f" (gave up after {len(delays)} retries)"
    raise _EndpointFailed(_redact(error, token))


def _counts(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    counts = {k: int(payload.get(k) or 0) for k in _COUNT_KEYS}
    rejected = payload.get("rejected") or []
    counts["rejected_items"] = rejected if isinstance(rejected, list) else []
    return counts


def _event(conn: db.Connection, run_id: int | None, message: str, level: str = "info",
           counters: dict[str, Any] | None = None) -> None:
    getattr(log, "warning" if level in ("warning", "error") else "info")(message)
    if run_id is not None:
        db.log_event(conn, run_id, message, stage="endpoint-sync", level=level,
                     counters=counters)


def _sync_one(
    conn: db.Connection, client: httpx.Client, endpoint: dict[str, Any], *,
    dry_run: bool, limit: int | None, sleep: Callable[[float], Any],
    run_id: int | None,
) -> dict[str, Any]:
    name, url, token = endpoint["name"], endpoint["url"], endpoint["token"]
    stats: dict[str, Any] = {
        "name": name, "status": "ok", "sent": 0, "rejected": 0, "oversized": 0,
        **{k: 0 for k in _COUNT_KEYS}, "message": "",
    }
    cursor = (
        (endpoint["cursor_changed_at"], endpoint["cursor_source"],
         endpoint["cursor_source_job_id"])
        if endpoint["cursor_changed_at"] else None
    )
    # Decided once per pass, not per page: a capped or multi-page first sync
    # must stay "open rows only" until it is done.
    first_sync = cursor is None
    saved_cursor = cursor
    remaining = limit

    def flush(serialized: list[str], key: tuple[str, str, str]) -> None:
        nonlocal saved_cursor
        if dry_run:
            stats["sent"] += len(serialized)
            return
        response = _post(client, url, token, _body(serialized), sleep)
        counts = _counts(response)
        stats["sent"] += len(serialized)
        for k in _COUNT_KEYS:
            stats[k] += counts[k]
        rejected = counts["rejected_items"]
        if rejected:
            # Never re-sent: the receiver judged these rows, the cursor moves on.
            stats["rejected"] += len(rejected)
            details = "; ".join(
                f"{item.get('externalId', '?')}: {item.get('message', '')}"
                if isinstance(item, dict) else str(item)
                for item in rejected[:_MAX_REJECTED_LOGGED]
            )
            _event(conn, run_id, _redact(
                f"endpoint-sync {name}: {len(rejected)} rejected: {details}", token,
            ), level="warning")
        db.save_endpoint_cursor(conn, endpoint["id"], *key)
        saved_cursor = key

    while remaining is None or remaining > 0:
        page_size = _PAGE_SIZE if remaining is None else min(_PAGE_SIZE, remaining)
        params: list[Any] = [*cursor, page_size] if cursor else [page_size]
        page = conn.execute(_select_sql(first_sync, cursor is not None), tuple(params)).fetchall()
        if not page:
            break
        if remaining is not None:
            remaining -= len(page)

        pending: list[str] = []
        consumed: tuple[str, str, str] | None = None
        for job_row in page:
            key = (job_row["changed_at_text"], job_row["source"], job_row["source_job_id"])
            serialized = json.dumps(_to_row(job_row))
            if len(_body([serialized])) > _MAX_BODY_BYTES:
                stats["oversized"] += 1
                _event(conn, run_id,
                       f"endpoint-sync {name}: skipped {key[1]}:{key[2]} — "
                       f"{len(serialized)} bytes is over the {_MAX_BODY_BYTES}-byte limit",
                       level="warning")
                consumed = key
                continue
            if pending and (
                len(pending) >= _MAX_ROWS_PER_REQUEST
                or len(_body([*pending, serialized])) > _MAX_BODY_BYTES
            ):
                flush(pending, consumed)
                pending = []
            pending.append(serialized)
            consumed = key
        if pending:
            flush(pending, consumed)
        elif consumed and consumed != saved_cursor and not dry_run:
            # Only oversized rows were left: move past them without a request.
            db.save_endpoint_cursor(conn, endpoint["id"], *consumed)
            saved_cursor = consumed
        cursor = (page[-1]["changed_at_text"], page[-1]["source"], page[-1]["source_job_id"])
        if len(page) < page_size:
            break

    announce = {k: stats[k] for k in _ANNOUNCE_KEYS}
    if not dry_run and sum(announce.values()) > 0:
        _post(client, url, token,
              json.dumps({"rows": [], "announce": announce}).encode(), sleep)

    stats["message"] = (
        f"sent {stats['sent']}: {stats['inserted']} inserted, {stats['updated']} updated, "
        f"{stats['reopened']} reopened, {stats['closed']} closed, "
        f"{stats['skipped']} skipped, {stats['rejected']} rejected"
        + (f", {stats['oversized']} oversized" if stats["oversized"] else "")
        + (" (dry run)" if dry_run else "")
    )
    return stats


def sync(
    conn: db.Connection,
    *,
    client: httpx.Client | None = None,
    endpoint_name: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    run_id: int | None = None,
) -> dict[str, Any]:
    """One pass over every enabled endpoint (or just `endpoint_name`).

    `dry_run` sends nothing and saves no cursor or status. `limit` still
    sends for real; it caps rows per endpoint for this run, and the cursor
    covers exactly what was sent.
    """
    with _locked(conn):
        endpoints = db.enabled_endpoints(conn, name=endpoint_name)
        own_client = client is None
        if own_client:
            client = _make_client()
        results: list[dict[str, Any]] = []
        try:
            for endpoint in endpoints:
                try:
                    stats = _sync_one(conn, client, endpoint, dry_run=dry_run,
                                      limit=limit, sleep=sleep, run_id=run_id)
                except _EndpointFailed as exc:
                    stats = {"name": endpoint["name"], "status": "error", "message": str(exc)}
                except Exception as exc:  # noqa: BLE001 - one endpoint must not stop the rest
                    log.exception("endpoint-sync %s: unexpected failure", endpoint["name"])
                    stats = {"name": endpoint["name"], "status": "error",
                             "message": _redact(f"{type(exc).__name__}: {exc}", endpoint["token"])}
                level = "info" if stats["status"] == "ok" else "error"
                _event(conn, run_id, f"endpoint-sync {endpoint['name']}: {stats['message']}",
                       level=level, counters={k: v for k, v in stats.items() if k != "message"})
                if not dry_run:
                    db.record_endpoint_sync(conn, endpoint["id"], stats["status"], stats["message"])
                results.append(stats)
        finally:
            if own_client:
                client.close()
    return {
        "endpoints": results,
        "failed": sum(1 for r in results if r["status"] != "ok"),
        "sent": sum(r.get("sent", 0) for r in results),
        "dry_run": dry_run,
    }


def test_connection(url: str, token: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """POST an empty batch once — no retries, nothing saved. For the
    dashboard's "Test connection" button."""
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=_TEST_TIMEOUT_SECONDS, follow_redirects=False)
    try:
        response = _post(client, url, token, b'{"rows": []}', lambda s: None,
                         timeout=_TEST_TIMEOUT_SECONDS, retry=False)
    except _EndpointFailed as exc:
        return {"ok": False, "message": str(exc)}
    finally:
        if own_client:
            client.close()
    return {"ok": True, "status": response.status_code,
            "message": f"HTTP {response.status_code}"}
