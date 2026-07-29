"""One-time migration of the archive from SQLite to PostgreSQL.

Reads the legacy ``data/jobs.db`` file directly with the stdlib ``sqlite3``
module — the only place in the app that still touches SQLite — and writes into
the already-provisioned Postgres schema (``db.connect()`` runs the DDL before
this ever executes). Every insert is ``ON CONFLICT DO NOTHING``/``DO UPDATE``,
so re-running this after an interruption just re-processes already-migrated
rows harmlessly rather than erroring or duplicating.

Jobs are round-tripped through the real :class:`~jobscrape.models.Job`
dataclass rather than copied column-for-column, so migrated rows get the same
``company_slug`` derivation and ``role_fingerprint``/``role_key`` computation
that a live fetch would produce — the fingerprints a fresh archive gets are
exactly the fingerprints a migrated one gets.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from . import db
from . import fingerprint as fp
from .models import Job

log = logging.getLogger("jobscrape.migrate")

_BATCH = 500


def _sqlite_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _chunks(rows: list[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _migrate_companies(src: sqlite3.Connection, conn: db.Connection, *, dry_run: bool) -> int:
    rows = src.execute("SELECT * FROM companies").fetchall()
    if dry_run or not rows:
        return len(rows)
    for batch in _chunks(rows, _BATCH):
        payload = [
            (r["ats"], r["slug"], r["name"], r["extra"], r["status"], r["job_count"] or 0,
             r["last_checked"], r["discovered_at"])
            for r in batch
        ]
        conn.executemany(
            "INSERT INTO companies (ats, slug, name, extra, status, job_count, last_checked, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (ats, slug) DO UPDATE SET name=excluded.name, extra=excluded.extra, "
            "status=excluded.status, job_count=excluded.job_count, last_checked=excluded.last_checked",
            payload,
        )
        conn.commit()
    return len(rows)


def _migrate_jobs(src: sqlite3.Connection, conn: db.Connection, *, dry_run: bool) -> int:
    rows = src.execute("SELECT * FROM jobs").fetchall()
    if dry_run or not rows:
        return len(rows)
    placeholders = ", ".join("?" * (len(db.JOB_COLUMNS) + 6))
    sql = (
        f"INSERT INTO jobs ({', '.join(db.JOB_COLUMNS)}, first_seen_at, last_seen_at, closed_at, "
        f"role_fingerprint, role_key, content_hash) VALUES ({placeholders}) "
        f"ON CONFLICT (source, source_job_id) DO NOTHING"
    )
    total = 0
    for batch in _chunks(rows, _BATCH):
        payload = []
        for r in batch:
            job = Job(
                source=r["source"], source_job_id=r["source_job_id"], title=r["title"],
                company=r["company"] or "", job_url=r["job_url"],
                apply_url=r["apply_url"], company_slug=r["company_slug"],
                location_raw=r["location_raw"], city=r["city"], state=r["state"],
                country=r["country"],
                is_remote=None if r["is_remote"] is None else bool(r["is_remote"]),
                workplace_type=r["workplace_type"], employment_type=r["employment_type"],
                department=r["department"], posted_at=r["posted_at"], updated_at=r["updated_at"],
                salary_min=r["salary_min"], salary_max=r["salary_max"],
                salary_currency=r["salary_currency"], salary_raw=r["salary_raw"],
                description_html=r["description_html"], description_text=r["description_text"],
                raw=json.loads(r["raw_json"]) if r["raw_json"] else {},
            )
            row = job.to_row()
            # The SQLite archive never recorded the moment a posting actually
            # closed, only a boolean — `last_seen_at` (the last successful
            # fetch that still listed it) is the best available approximation.
            closed_at = None if (r["is_open"] in (1, None)) else r["last_seen_at"]
            payload.append(
                [row.get(c) for c in db.JOB_COLUMNS]
                + [r["first_seen_at"], r["last_seen_at"], closed_at,
                   fp.role_fingerprint(job), fp.role_key(job), fp.content_hash(job)]
            )
        conn.executemany(sql, payload)
        conn.commit()
        total += len(batch)
        log.info("jobs: migrated %d/%d", total, len(rows))
    return total


def _migrate_job_status(src: sqlite3.Connection, conn: db.Connection, *, dry_run: bool) -> int:
    rows = src.execute("SELECT * FROM job_status").fetchall()
    if dry_run or not rows:
        return len(rows)
    for batch in _chunks(rows, _BATCH):
        payload = [
            (r["source"], r["source_job_id"], r["status"], r["notes"], r["updated_at"])
            for r in batch
        ]
        conn.executemany(
            "INSERT INTO job_status (source, source_job_id, status, notes, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (source, source_job_id) DO NOTHING",
            payload,
        )
        conn.commit()
    return len(rows)


def _migrate_posting_seen(src: sqlite3.Connection, conn: db.Connection, *, dry_run: bool) -> int:
    if not _table_exists(src, "posting_seen"):
        return 0
    rows = src.execute("SELECT * FROM posting_seen").fetchall()
    if dry_run or not rows:
        return len(rows)
    cols = rows[0].keys()
    has_reason = "reason" in cols
    for batch in _chunks(rows, _BATCH):
        payload = [
            (r["source"], r["source_job_id"], r["company_slug"], r["posted_at"],
             r["evaluated_at"], r["reason"] if has_reason else "rejected")
            for r in batch
        ]
        conn.executemany(
            "INSERT INTO posting_seen (source, source_job_id, company_slug, posted_at, evaluated_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (source, source_job_id) DO NOTHING",
            payload,
        )
        conn.commit()
    return len(rows)


def _migrate_runs(src: sqlite3.Connection, conn: db.Connection, *, dry_run: bool) -> tuple[int, int]:
    rows = src.execute("SELECT * FROM runs").fetchall()
    if dry_run:
        events = src.execute("SELECT COUNT(*) c FROM run_events").fetchone()
        return len(rows), events["c"]
    id_map: dict[int, int] = {}
    for r in rows:
        result = conn.execute(
            "INSERT INTO runs (command, stage, started_at, ended_at, status, stats, pid, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (r["command"], r["stage"], r["started_at"], r["ended_at"], r["status"],
             r["stats"], r["pid"], r["error"]),
        )
        id_map[r["id"]] = result.scalar()
    conn.commit()

    events = src.execute("SELECT * FROM run_events").fetchall()
    migrated_events = 0
    for batch in _chunks(events, _BATCH):
        payload = [
            (id_map[r["run_id"]], r["ts"], r["stage"], r["source"], r["level"],
             r["message"], r["counters"])
            for r in batch if r["run_id"] in id_map
        ]
        if payload:
            conn.executemany(
                "INSERT INTO run_events (run_id, ts, stage, source, level, message, counters) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
            conn.commit()
        migrated_events += len(payload)
    return len(rows), migrated_events


def migrate(
    sqlite_path: Path, database_url: str | None = None, *, dry_run: bool = False
) -> dict[str, int]:
    """Copy every table from the SQLite archive into Postgres.

    Returns row counts per table so the move is verifiable rather than hoped
    for. ``dry_run`` reports counts without writing anything.
    """
    if not sqlite_path.exists():
        raise FileNotFoundError(f"no SQLite archive at {sqlite_path}")

    src = _sqlite_conn(sqlite_path)
    try:
        with db.session(database_url) as conn:
            companies = _migrate_companies(src, conn, dry_run=dry_run)
            jobs = _migrate_jobs(src, conn, dry_run=dry_run)
            job_status = _migrate_job_status(src, conn, dry_run=dry_run)
            posting_seen = _migrate_posting_seen(src, conn, dry_run=dry_run)
            runs, run_events = _migrate_runs(src, conn, dry_run=dry_run)
    finally:
        src.close()

    return {
        "companies": companies, "jobs": jobs, "job_status": job_status,
        "posting_seen": posting_seen, "runs": runs, "run_events": run_events,
    }
