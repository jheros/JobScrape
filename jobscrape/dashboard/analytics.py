"""Read-only queries behind the dashboard.

Every function takes an open connection and returns plain dicts/lists, so the
routes stay thin and each query can be tested without a web server.

"Open" means ``closed_at IS NULL``; every count and listing here also excludes
suppressed duplicates (``duplicate_of IS NOT NULL``) unless a function is
explicitly about duplicates — see ``jobscrape.dedupe``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .. import db, seniority as seniority_mod, staleness

#: Every "current" job listing filters through this. Not applied to queries
#: that are explicitly about closed jobs or about duplicates themselves.
_OPEN = "closed_at IS NULL AND duplicate_of IS NULL"


def _rows(conn: db.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params)]


def _scalar(conn: db.Connection, sql: str, params: tuple = (), default: Any = 0) -> Any:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return default
    value = next(iter(row.values()))
    return default if value is None else value


# --- overview -------------------------------------------------------------

def overview(conn: db.Connection) -> dict[str, Any]:
    """Headline numbers for the landing page."""
    open_jobs = _scalar(conn, f"SELECT COUNT(*) c FROM jobs WHERE {_OPEN}")
    total = _scalar(conn, "SELECT COUNT(*) c FROM jobs WHERE duplicate_of IS NULL")

    # Untriaged *open* jobs — the number that actually matters daily.
    untriaged = _scalar(
        conn,
        "SELECT COUNT(*) c FROM jobs j LEFT JOIN job_status s"
        "  ON s.source = j.source AND s.source_job_id = j.source_job_id "
        "WHERE j.closed_at IS NULL AND j.duplicate_of IS NULL "
        "AND (s.status IS NULL OR s.status = 'new')",
    )

    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    last = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()

    return {
        "total_jobs": total,
        "open_jobs": open_jobs,
        "closed_jobs": total - open_jobs,
        "untriaged": untriaged,
        "duplicates_collapsed": db.duplicate_count(conn),
        "new_24h": _scalar(
            conn, "SELECT COUNT(*) c FROM jobs WHERE first_seen_at >= ? AND duplicate_of IS NULL",
            (day_ago,),
        ),
        "new_7d": _scalar(
            conn, "SELECT COUNT(*) c FROM jobs WHERE first_seen_at >= ? AND duplicate_of IS NULL",
            (week_ago,),
        ),
        "live_boards": _scalar(conn, "SELECT COUNT(*) c FROM companies WHERE status='live'"),
        "unchecked_boards": _scalar(
            conn, "SELECT COUNT(*) c FROM companies WHERE status='unchecked'"
        ),
        "total_companies": _scalar(conn, "SELECT COUNT(*) c FROM companies"),
        "sources": _scalar(conn, "SELECT COUNT(DISTINCT source) c FROM jobs"),
        "companies_hiring": _scalar(
            conn, f"SELECT COUNT(DISTINCT company) c FROM jobs WHERE {_OPEN}"
        ),
        "last_run": dict(last) if last else None,
        "status_counts": db.status_counts(conn),
    }


def next_action(data: dict[str, Any]) -> dict[str, str]:
    """Suggest the single most useful next step.

    Ordered by leverage: an empty database needs a sweep; a large unvalidated
    backlog is the biggest lever on coverage; otherwise just refresh.
    """
    if not data["total_jobs"]:
        return {
            "label": "Run the first full sweep",
            "detail": "No jobs collected yet. This discovers boards, validates them, "
                      "then fetches every open posting.",
            "action": "sweep",
        }
    if data["unchecked_boards"] > 500:
        return {
            "label": f"Validate {data['unchecked_boards']:,} unchecked boards",
            "detail": "Discovered companies contribute nothing until they are probed. "
                      "This is usually the largest single gain available.",
            "action": "validate",
        }
    if data["untriaged"] > 0:
        return {
            "label": f"Review {data['untriaged']:,} untriaged jobs",
            "detail": "Work through the new listings and mark what you save or apply to.",
            "action": "jobs",
        }
    return {
        "label": "Refresh now",
        "detail": "Everything is triaged. Fetch again to pick up newly posted jobs.",
        "action": "fetch",
    }


# --- time series ----------------------------------------------------------

def jobs_over_time(conn: db.Connection, days: int = 30) -> list[dict[str, Any]]:
    """Jobs first seen per day — the growth curve of the archive."""
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=days)).isoformat()
    found = {
        str(r["d"]): r["c"]
        for r in conn.execute(
            "SELECT (first_seen_at AT TIME ZONE 'UTC')::date d, COUNT(*) c FROM jobs "
            "WHERE (first_seen_at AT TIME ZONE 'UTC')::date >= ? AND duplicate_of IS NULL "
            "GROUP BY d",
            (start,),
        )
    }
    # Fill gaps so the chart has no misleading holes.
    series = []
    for offset in range(days + 1):
        day = (today - timedelta(days=days - offset)).isoformat()
        series.append({"date": day, "count": found.get(day, 0)})
    return series


def postings_by_date(conn: db.Connection, days: int = 30) -> list[dict[str, Any]]:
    """When the jobs were *posted*, as opposed to when we saw them."""
    start = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    rows = _rows(
        conn,
        f"SELECT (posted_at AT TIME ZONE 'UTC')::date d, COUNT(*) c FROM jobs "
        f"WHERE posted_at IS NOT NULL AND (posted_at AT TIME ZONE 'UTC')::date >= ? "
        f"AND {_OPEN} GROUP BY d ORDER BY d",
        (start,),
    )
    for r in rows:
        r["d"] = str(r["d"])
    return rows


# --- breakdowns -----------------------------------------------------------

def by_source(conn: db.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT source, COUNT(*) total,"
        "       SUM(CASE WHEN closed_at IS NULL THEN 1 ELSE 0 END) open,"
        "       SUM(CASE WHEN salary_min IS NOT NULL THEN 1 ELSE 0 END) with_salary,"
        "       COUNT(DISTINCT company) companies "
        "FROM jobs WHERE duplicate_of IS NULL GROUP BY source ORDER BY total DESC",
    )


def by_workplace(conn: db.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT COALESCE(workplace_type,'unknown') workplace, COUNT(*) c "
        f"FROM jobs WHERE {_OPEN} GROUP BY workplace ORDER BY c DESC",
    )


def by_state(conn: db.Connection, limit: int = 12) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT state, COUNT(*) c FROM jobs "
        f"WHERE state IS NOT NULL AND {_OPEN} GROUP BY state ORDER BY c DESC LIMIT ?",
        (limit,),
    )


def top_companies(conn: db.Connection, limit: int = 12) -> list[dict[str, Any]]:
    return _rows(
        conn,
        f"SELECT company, COUNT(*) c FROM jobs WHERE {_OPEN} "
        "GROUP BY company ORDER BY c DESC LIMIT ?",
        (limit,),
    )


def coverage(conn: db.Connection) -> list[dict[str, Any]]:
    """The discovery -> validation funnel, per ATS."""
    return _rows(
        conn,
        "SELECT ats,"
        "  COUNT(*) discovered,"
        "  SUM(CASE WHEN status='live' THEN 1 ELSE 0 END) live,"
        "  SUM(CASE WHEN status='empty' THEN 1 ELSE 0 END) empty,"
        "  SUM(CASE WHEN status='dead' THEN 1 ELSE 0 END) dead,"
        "  SUM(CASE WHEN status='unchecked' THEN 1 ELSE 0 END) unchecked "
        "FROM companies GROUP BY ats ORDER BY live DESC, discovered DESC",
    )


#: The nine fields a job is expected to carry, as SQL predicates over an open
#: posting. Mirrors ``export.REQUIRED_FIELDS`` — a field satisfied by any of a
#: set of columns is written as an OR here too, so a location supplied as
#: structured city/state counts even when `location_raw` is blank.
#:
#: `unknown` is deliberately NOT counted as populated for mode and seniority:
#: it is the value meaning "we could not tell", and counting it would report
#: 100% coverage of a column that is half empty.
FIELD_COVERAGE = {
    "title": "title IS NOT NULL AND title <> ''",
    "source": "source IS NOT NULL",
    "location": "COALESCE(location_raw, city, state) IS NOT NULL",
    "mode": "workplace_type IS NOT NULL AND workplace_type <> 'unknown'",
    "seniority": "seniority IS NOT NULL AND seniority <> 'unknown'",
    "salary": "salary_min IS NOT NULL OR salary_max IS NOT NULL",
    "skills": "skills IS NOT NULL AND skills <> ''",
    # A NULL check, not a length check. `length(description_text)` forces
    # Postgres to decompress every stored description — measured at 6.3s
    # against 0.2s for the NULL test, to change the answer by 58 rows in
    # 37,989 (0.15%). A page that takes six seconds to say the same thing is
    # the wrong trade.
    "description": "description_text IS NOT NULL",
    "link": "COALESCE(job_url, apply_url) IS NOT NULL",
}


def field_coverage(conn: db.Connection) -> list[dict[str, Any]]:
    """How completely each of the nine expected fields is populated.

    Surfaced because the gaps are real and permanent: roughly half of all
    postings never state a work mode and a third never state pay. Reporting
    that is more useful than quietly showing a blank column, and it is the only
    way to tell "the scraper missed it" from "the employer never said".
    """
    columns = ", ".join(
        f"count(*) FILTER (WHERE {predicate}) AS {name}"
        for name, predicate in FIELD_COVERAGE.items()
    )
    row = conn.execute(
        f"SELECT count(*) AS total, {columns} FROM jobs WHERE {_OPEN}"
    ).fetchone()
    total = row["total"] or 0
    complete = _scalar(
        conn,
        "SELECT count(*) c FROM jobs WHERE " + _OPEN + " AND "
        + " AND ".join(f"({p})" for p in FIELD_COVERAGE.values()),
    )
    out = [
        {"field": name, "count": row[name],
         "pct": round(100 * row[name] / total) if total else 0}
        for name in FIELD_COVERAGE
    ]
    out.append({"field": "all nine", "count": complete,
                "pct": round(100 * complete / total) if total else 0})
    return out


def salary_coverage(conn: db.Connection) -> dict[str, Any]:
    total = _scalar(conn, f"SELECT COUNT(*) c FROM jobs WHERE {_OPEN}")
    with_salary = _scalar(
        conn, f"SELECT COUNT(*) c FROM jobs WHERE {_OPEN} AND salary_min IS NOT NULL"
    )
    median = _scalar(
        conn,
        f"SELECT AVG(salary_min) c FROM jobs WHERE {_OPEN} AND salary_min BETWEEN 30000 AND 900000",
        default=0,
    )
    return {
        "total": total,
        "with_salary": with_salary,
        "pct": round(100 * with_salary / total) if total else 0,
        "avg_min": round(median or 0),
    }


# --- runs -----------------------------------------------------------------

def run_history(conn: db.Connection, limit: int = 20) -> list[dict[str, Any]]:
    runs = []
    for row in db.recent_runs(conn, limit):
        item = dict(row)
        stats = item.get("stats")
        if isinstance(stats, str):
            try:
                item["stats_parsed"] = json.loads(stats or "{}")
            except json.JSONDecodeError:
                item["stats_parsed"] = {}
        else:
            item["stats_parsed"] = stats or {}
        item["duration"] = _duration(item.get("started_at"), item.get("ended_at"))
        runs.append(item)
    return runs


def _duration(started: Any, ended: Any) -> str:
    if not started:
        return ""
    try:
        a = started if isinstance(started, datetime) else datetime.fromisoformat(str(started))
        if ended:
            b = ended if isinstance(ended, datetime) else datetime.fromisoformat(str(ended))
        else:
            b = datetime.now(timezone.utc)
    except ValueError:
        return ""
    seconds = int((b - a).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _stats_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def run_trend(conn: db.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """New/refreshed jobs per fetch run, oldest first — a trend, not just the
    last run's snapshot. Reads straight out of `runs.stats`, which storing as
    JSONB (rather than the old opaque TEXT blob) is what makes this a simple
    query instead of loading and parsing every row in Python."""
    rows = [
        {"run_id": r["id"], "rows_touched": (_stats_dict(r["stats"]) or {}).get("rows_touched", 0)}
        for r in db.recent_runs(conn, limit)
        if r["stage"] == "fetch" and r["status"] == "ok"
    ]
    return list(reversed(rows))


def latest_funnel(conn: db.Connection) -> list[dict[str, Any]]:
    """Per-source filter funnel from the most recent fetch that recorded one."""
    for row in db.recent_runs(conn, 20):
        stats = _stats_dict(row["stats"])
        sources = stats.get("sources") or {}
        if not sources:
            continue
        out = []
        for name, s in sources.items():
            if not isinstance(s, dict) or not s.get("seen"):
                continue
            out.append({
                "source": name,
                "seen": s.get("seen", 0),
                "dropped_role": s.get("dropped_role", 0),
                "dropped_location": s.get("dropped_location", 0),
                "dropped_window": s.get("dropped_window", 0),
                "kept": s.get("kept", 0),
            })
        if out:
            return sorted(out, key=lambda r: -r["seen"])
    return []


def http_health(conn: db.Connection) -> dict[str, Any]:
    """Request/cache/rate-limit counters from the most recent run that has them."""
    for row in db.recent_runs(conn, 20):
        stats = _stats_dict(row["stats"])
        http = stats.get("http") or {}
        if http:
            ok = http.get("ok", 0)
            cached = http.get("cache_hit", 0)
            total = ok + cached
            return {
                "ok": ok,
                "cache_hit": cached,
                "cache_pct": round(100 * cached / total) if total else 0,
                "rate_limited": sum(v for k, v in http.items() if "429" in k),
                "failed": http.get("failed", 0),
                "errors": {k: v for k, v in http.items() if k.startswith("http_")},
                "run_id": row["id"],
            }
    return {}


# --- jobs browser ---------------------------------------------------------

JOB_SORTS = {
    "newest": "j.first_seen_at DESC, j.id DESC",
    "posted": "j.posted_at DESC NULLS LAST",
    "salary": "j.salary_max DESC NULLS LAST",
    "company": "j.company ASC",
}


#: Mirrors `render_sync.py`'s own `_CHANGED_SINCE_CTE` `changed_at` expression
#: exactly, so this page can never disagree with render_sync about what
#: "changed" means. 'blocked' beats 'synced' when both could apply (a row
#: last blocked, then never subsequently re-attempted, stays 'blocked' — its
#: `render_synced_at` IS the timestamp of that blocked attempt, so the
#: `>=` check in the 'synced' branch would also be true; ordering the WHEN
#: clauses blocked-first is what makes 'blocked' win).
_RENDER_SYNC_STATE_SQL = """
    CASE
        WHEN j.render_sync_status = 'blocked'
             AND j.render_synced_at >= GREATEST(
                 j.last_seen_at, COALESCE(j.closed_at, j.last_seen_at),
                 COALESCE(j.duplicate_marked_at, j.last_seen_at))
            THEN 'blocked'
        WHEN j.render_synced_at IS NOT NULL
             AND j.render_synced_at >= GREATEST(
                 j.last_seen_at, COALESCE(j.closed_at, j.last_seen_at),
                 COALESCE(j.duplicate_marked_at, j.last_seen_at))
            THEN 'synced'
        ELSE 'pending'
    END
"""


def search_jobs(
    conn: db.Connection,
    *,
    status: str | None = "new",
    source: str | None = None,
    state: str | None = None,
    workplace: str | None = None,
    seniority: str | None = None,
    skill: str | None = None,
    query: str | None = None,
    company: str | None = None,
    employment_type: str | None = None,
    department: str | None = None,
    industry: str | None = None,
    is_remote: bool | None = None,
    min_salary: float | None = None,
    max_salary: float | None = None,
    posted_since: date | None = None,
    posted_until: date | None = None,
    scam_signals: bool | None = None,
    sync_state: str | None = None,
    stale: str | None = None,
    include_closed: bool = False,
    include_duplicates: bool = False,
    sort: str = "newest",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Filtered job list plus the total match count, for pagination."""
    where = ["1=1"]
    params: list[Any] = []

    if not include_duplicates:
        where.append("j.duplicate_of IS NULL")
    if not include_closed:
        where.append("j.closed_at IS NULL")
    if status and status != "all":
        if status == "new":
            # Never-triaged rows have no job_status entry at all.
            where.append("(s.status IS NULL OR s.status = 'new')")
        else:
            where.append("s.status = ?")
            params.append(status)
    if source:
        where.append("j.source = ?")
        params.append(source)
    if state:
        where.append("j.state = ?")
        params.append(state)
    if workplace:
        where.append("COALESCE(j.workplace_type,'unknown') = ?")
        params.append(workplace)
    if seniority:
        where.append("COALESCE(j.seniority,'unknown') = ?")
        params.append(seniority)
    if skill:
        # Whole-element match, not a substring — a bare LIKE '%go%' would hit
        # "django" and "mongodb". Array containment rather than a padded LIKE
        # because it can use the GIN index on `string_to_array(skills, ',')`:
        # unindexed, this filter combined with the staleness cluster join took
        # long enough to hang a page load; indexed, it is 0.03s.
        where.append("string_to_array(j.skills, ',') @> ARRAY[?]")
        params.append(skill)
    if company:
        where.append("j.company ILIKE ?")
        params.append(f"%{company}%")
    if employment_type:
        where.append("j.employment_type = ?")
        params.append(employment_type)
    if department:
        where.append("j.department = ?")
        params.append(department)
    if industry:
        where.append("j.industry = ?")
        params.append(industry)
    if is_remote is not None:
        where.append("j.is_remote = ?")
        params.append(is_remote)
    if min_salary:
        # Deliberately `salary_max >= ?`, not `salary_min >= ?`: "does this
        # job's range reach at least this floor" rather than "does its own
        # minimum start there" — a wide range that could pay what you want
        # shouldn't be hidden just because its own stated floor is lower.
        # `max_salary` below is the new, additive paired filter; this
        # existing field's own meaning is intentionally left alone.
        where.append("j.salary_max >= ?")
        params.append(min_salary)
    if max_salary:
        where.append("j.salary_min <= ?")
        params.append(max_salary)
    if posted_since:
        # Cast explicitly to UTC before taking the date, not a bare
        # `posted_at >= ?` — the session timezone here is NOT UTC (observed
        # Asia/Vientiane, UTC+7), and comparing a TIMESTAMPTZ against a bare
        # DATE literal implicitly interprets that literal at session-local
        # midnight, silently shifting the boundary by a full session-UTC
        # offset from what `posted_at`'s own UTC-stored values mean.
        where.append("(j.posted_at AT TIME ZONE 'UTC')::date >= ?")
        params.append(posted_since)
    if posted_until:
        where.append("(j.posted_at AT TIME ZONE 'UTC')::date <= ?")
        params.append(posted_until)
    if scam_signals:
        where.append("j.scam_signals <> ''")
    if sync_state:
        where.append(f"{_RENDER_SYNC_STATE_SQL} = ?")
        params.append(sync_state)
    if query:
        where.append("(j.title ILIKE ? OR j.company ILIKE ? OR j.description_text ILIKE ?)")
        like = f"%{query}%"
        params.extend([like, like, like])

    # The staleness verdict is computed, not stored, so filtering by it has to
    # happen here rather than in Python — otherwise the page would be truncated
    # before the filter ran and pagination would be wrong. The SQL is generated
    # from staleness.py's own constants; see the drift test there.
    # The verdict reads stored columns only, so it binds no parameters of its
    # own and can appear in both the SELECT list and the WHERE clause freely.
    verdict_sql = staleness.sql_verdict()
    if stale:
        where.append(f"{verdict_sql} = ?")
        params.append(stale)

    clause = " AND ".join(where)
    base = (
        f"FROM jobs j {staleness.CLUSTER_JOIN} LEFT JOIN job_status s "
        "  ON s.source = j.source AND s.source_job_id = j.source_job_id "
        f"WHERE {clause}"
    )
    total = _scalar(conn, f"SELECT COUNT(*) c {base}", tuple(params))
    order = JOB_SORTS.get(sort, JOB_SORTS["newest"])
    rows = _rows(
        conn,
        f"SELECT j.*, COALESCE(s.status,'new') AS status, s.notes, "
        f"       {verdict_sql} AS staleness, "
        f"       {_RENDER_SYNC_STATE_SQL} AS render_sync_state {base} "
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    return rows, total


def filter_options(conn: db.Connection) -> dict[str, list[Any]]:
    """Values that actually occur, so the filters never offer an empty result."""
    return {
        "sources": [r["source"] for r in conn.execute(
            "SELECT DISTINCT source FROM jobs ORDER BY source")],
        "states": [r["state"] for r in conn.execute(
            "SELECT DISTINCT state FROM jobs WHERE state IS NOT NULL ORDER BY state")],
        "workplaces": [r["w"] for r in conn.execute(
            "SELECT DISTINCT COALESCE(workplace_type,'unknown') w FROM jobs ORDER BY w")],
        # Ordered by the seniority ladder, not alphabetically — "director,
        # entry, executive, intern" tells a reader nothing about level.
        "seniorities": sorted(
            (r["s"] for r in conn.execute(
                "SELECT DISTINCT COALESCE(seniority,'unknown') s FROM jobs")),
            key=seniority_mod.sort_key,
        ),
        # Most-common first: a dropdown of 133 alphabetised technologies is
        # unusable, and the popular ones are what people filter by.
        "skills": [r["skill"] for r in conn.execute(
            "SELECT skill, count(*) n FROM ("
            "  SELECT unnest(string_to_array(skills, ',')) skill FROM jobs"
            f"  WHERE {_OPEN} AND skills <> ''"
            ") t WHERE skill <> '' GROUP BY skill ORDER BY n DESC, skill LIMIT 60")],
        "employment_types": [r["v"] for r in conn.execute(
            "SELECT DISTINCT employment_type v FROM jobs "
            "WHERE employment_type IS NOT NULL ORDER BY v")],
        "departments": [r["v"] for r in conn.execute(
            "SELECT DISTINCT department v FROM jobs "
            "WHERE department IS NOT NULL ORDER BY v")],
        "industries": [r["v"] for r in conn.execute(
            "SELECT DISTINCT industry v FROM jobs "
            "WHERE industry IS NOT NULL ORDER BY v")],
    }
