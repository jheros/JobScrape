"""FastAPI application for the management dashboard.

Routes stay thin: queries live in :mod:`analytics`, process control in
:mod:`runner`, and SVG generation in :mod:`charts`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime as datetime_cls
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator

from .. import config, db, scam
from ..observability import health as health_check
from . import analytics, charts
from .runner import STAGES, manager

log = logging.getLogger("jobscrape.dashboard")


def _blank_to_none(value: Any) -> Any:
    """Treat an empty query parameter as absent rather than as a bad value.

    An HTML form submits **every** named field, so a number input the user left
    alone arrives as ``""``. Pydantic cannot parse that as a float and rejects
    the whole request with a 422 — which is what the Jobs page did on every
    press of Apply, because leaving "Min salary" blank is the normal case. The
    filters never ran; the user got raw JSON instead of results.

    Only blank is reinterpreted. ``min_salary=abc`` still fails validation, as
    it should.
    """
    return None if isinstance(value, str) and not value.strip() else value


#: Numeric query parameters reachable from an HTML form. Any `<input
#: type="number">` needs one of these, or it reintroduces the 422 above.
BlankableFloat = Annotated[float | None, BeforeValidator(_blank_to_none)]
BlankableInt = Annotated[int | None, BeforeValidator(_blank_to_none)]

def _datefmt(value: Any, length: int = 16) -> str:
    """Format a timestamp for display, accepting either a `datetime` (what
    Postgres/psycopg2 hands back) or an ISO string — templates shouldn't need
    to know which."""
    if not value:
        return "—"
    text = value.isoformat() if isinstance(value, datetime_cls) else str(value)
    return text[:length].replace("T", " ")


def _scam_reasons(value: Any) -> str:
    """Turn the stored comma-separated signal names into a readable tooltip."""
    names = [n for n in str(value or "").split(",") if n]
    reasons = scam.scam_reasons(names)
    return "This posting " + "; ".join(reasons) if reasons else ""


HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["comma"] = lambda v: f"{int(v):,}" if v is not None else "0"
templates.env.filters["datefmt"] = _datefmt
templates.env.filters["scamreasons"] = _scam_reasons

# `openapi_url=None` too: disabling `docs_url`/`redoc_url` removes Swagger and
# ReDoc but FastAPI still serves the schema at /openapi.json, which enumerates
# every route including the unauthenticated mutating ones.
app = FastAPI(
    title="jobscrape dashboard", docs_url=None, redoc_url=None, openapi_url=None,
)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def _conn():
    return db.connect()


@app.get("/health")
async def health() -> JSONResponse:
    """Real dependency checks — Postgres connectivity/pool, last successful
    run's age, disk headroom — not just "the process is scheduled"."""
    result = health_check()
    return JSONResponse(result, status_code=200 if result["healthy"] else 503)


@app.on_event("startup")
async def _startup() -> None:
    # A dashboard restart must not leave phantom "running" rows from a previous
    # process, or every stage would look permanently busy.
    with db.session() as conn:
        closed = db.mark_stale_runs(conn)
        if closed:
            log.info("closed out %d stale run(s) from a previous session", closed)


def _page(request: Request, template: str, **context: Any) -> HTMLResponse:
    context.setdefault("active_runs", manager.active())
    context.setdefault("stages", list(STAGES))
    return templates.TemplateResponse(request, template, context)


# --- pages ----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def overview(request: Request) -> HTMLResponse:
    conn = _conn()
    try:
        data = analytics.overview(conn)
        timeline = analytics.jobs_over_time(conn, 30)
        return _page(
            request, "overview.html",
            data=data,
            action=analytics.next_action(data),
            timeline_svg=charts.area_chart(timeline, label="Jobs first seen per day"),
            spark=charts.sparkline([p["count"] for p in timeline[-14:]]),
            sources=analytics.by_source(conn),
            source_svg=charts.bar_chart(
                analytics.by_source(conn), label_key="source", value_key="total",
                label="Jobs by source",
            ),
            workplace=analytics.by_workplace(conn),
            workplace_svg=charts.single_stack(
                [(r["workplace"], r["c"]) for r in analytics.by_workplace(conn)],
                label="Workplace type",
            ),
            workplace_legend=charts.legend(
                [(r["workplace"], r["workplace"]) for r in analytics.by_workplace(conn)]
            ),
            salary=analytics.salary_coverage(conn),
            field_coverage=analytics.field_coverage(conn),
        )
    finally:
        conn.close()


@app.get("/jobs", response_class=HTMLResponse)
async def jobs(
    request: Request,
    status: str = "new",
    source: str | None = None,
    state: str | None = None,
    workplace: str | None = None,
    seniority: str | None = None,
    skill: str | None = None,
    q: str | None = None,
    min_salary: BlankableFloat = None,
    stale: str | None = None,
    include_closed: bool = False,
    sort: str = "newest",
    page: BlankableInt = 1,
) -> HTMLResponse:
    conn = _conn()
    try:
        per_page = 50
        # A blank `page=` now validates to None rather than 422-ing; an absent
        # or unusable page number means the first one.
        current = max(page or 1, 1)
        rows, total = analytics.search_jobs(
            conn, status=status, source=source, state=state, workplace=workplace,
            seniority=seniority, skill=skill,
            query=q, min_salary=min_salary, stale=stale, include_closed=include_closed,
            sort=sort, limit=per_page, offset=(current - 1) * per_page,
        )
        return _page(
            request, "jobs.html",
            rows=rows, total=total, page=current, per_page=per_page,
            pages=max((total + per_page - 1) // per_page, 1),
            options=analytics.filter_options(conn),
            statuses=db.JOB_STATUSES,
            filters={
                "status": status, "source": source or "", "state": state or "",
                "workplace": workplace or "", "seniority": seniority or "",
                "skill": skill or "", "q": q or "",
                "min_salary": min_salary or "", "stale": stale or "",
                "include_closed": include_closed,
                "sort": sort,
            },
        )
    finally:
        conn.close()


@app.get("/runs", response_class=HTMLResponse)
async def runs(request: Request) -> HTMLResponse:
    conn = _conn()
    try:
        trend = analytics.run_trend(conn, 20)
        return _page(
            request, "runs.html",
            history=analytics.run_history(conn, 25),
            http=analytics.http_health(conn),
            trend_svg=charts.bar_chart(
                trend, label_key="run_id", value_key="rows_touched",
                label="Jobs collected per fetch run", limit=20,
            ),
        )
    finally:
        conn.close()


@app.get("/coverage", response_class=HTMLResponse)
async def coverage(request: Request) -> HTMLResponse:
    conn = _conn()
    try:
        rows = analytics.coverage(conn)
        segments = [
            ("live", "Live"), ("unchecked", "Unchecked"),
            ("empty", "Empty"), ("dead", "Dead"),
        ]
        return _page(
            request, "coverage.html",
            rows=rows,
            coverage_svg=charts.stacked_bars(
                rows, label_key="ats", segments=segments,
                label="Board status by platform",
            ),
            coverage_legend=charts.legend(segments),
            totals={
                key: sum(int(r.get(key) or 0) for r in rows)
                for key in ("discovered", "live", "unchecked", "empty", "dead")
            },
        )
    finally:
        conn.close()


@app.get("/sources", response_class=HTMLResponse)
async def sources(request: Request) -> HTMLResponse:
    conn = _conn()
    try:
        funnel = analytics.latest_funnel(conn)
        segments = [
            ("kept", "Kept"), ("dropped_company", "Blocked employer"),
            ("dropped_role", "Not software"),
            ("dropped_location", "Not US-eligible"), ("dropped_window", "Outside window"),
            ("dropped_workplace", "Not remote"),
        ]
        return _page(
            request, "sources.html",
            sources=analytics.by_source(conn),
            funnel=funnel,
            funnel_svg=charts.stacked_bars(
                funnel, label_key="source", segments=segments,
                label="Why postings were dropped",
            ),
            funnel_legend=charts.legend(segments),
            companies=analytics.top_companies(conn),
            company_svg=charts.bar_chart(
                analytics.top_companies(conn), label_key="company", value_key="c",
                label="Companies with most open roles",
            ),
            states=analytics.by_state(conn),
            state_svg=charts.bar_chart(
                analytics.by_state(conn), label_key="state", value_key="c",
                label="Open roles by state",
            ),
            http=analytics.http_health(conn),
        )
    finally:
        conn.close()


# --- actions --------------------------------------------------------------

@app.post("/api/runs/{stage}/start")
async def start_stage(stage: str, extra: str = Form("")) -> JSONResponse:
    try:
        info = await manager.start(stage, extra.split() if extra.strip() else None)
    except (ValueError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse(info)


@app.post("/api/runs/{stage}/stop")
async def stop_stage(stage: str) -> JSONResponse:
    stopped = await manager.stop(stage)
    return JSONResponse({"stopped": stopped})


@app.get("/api/runs/active")
async def active_runs() -> JSONResponse:
    return JSONResponse({"runs": manager.active()})


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: int, after: int = 0) -> StreamingResponse:
    """Server-sent events tailing one run's log."""

    async def generate():
        cursor = after
        idle = 0
        while idle < 600:  # ~20 minutes of silence ends the stream
            conn = db.connect()
            try:
                rows = db.run_events(conn, run_id, cursor, limit=200)
                run = conn.execute(
                    "SELECT status FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
            finally:
                conn.close()

            if rows:
                idle = 0
                for row in rows:
                    cursor = row["id"]
                    payload = json.dumps({
                        "id": row["id"], "message": row["message"],
                        "level": row["level"], "ts": row["ts"],
                    })
                    yield f"data: {payload}\n\n"
            else:
                idle += 1

            if run and run["status"] != "running" and not rows:
                yield f"data: {json.dumps({'done': True, 'status': run['status']})}\n\n"
                return
            await asyncio.sleep(2)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/jobs/{source}/{source_job_id:path}/status")
async def set_status(
    source: str, source_job_id: str, status: str = Form(...), notes: str = Form("")
) -> JSONResponse:
    conn = _conn()
    try:
        db.set_job_status(conn, source, source_job_id, status, notes or None)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        conn.close()
    return JSONResponse({"ok": True, "status": status})


@app.post("/api/jobs/bulk")
async def bulk_status(ids: str = Form(...), status: str = Form(...)) -> JSONResponse:
    """Triage several rows at once — ids arrive as `source|job_id` pairs."""
    conn = _conn()
    changed = 0
    try:
        for token in ids.split(","):
            if "|" not in token:
                continue
            source, _, job_id = token.partition("|")
            db.set_job_status(conn, source, job_id, status)
            changed += 1
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        conn.close()
    return JSONResponse({"ok": True, "changed": changed})


@app.get("/api/stats")
async def api_stats() -> JSONResponse:
    conn = _conn()
    try:
        return JSONResponse(analytics.overview(conn))
    finally:
        conn.close()


def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    import uvicorn

    config.ensure_dirs()
    uvicorn.run(
        "jobscrape.dashboard.app:app" if reload else app,
        host=host, port=port, reload=reload, log_level="warning",
    )
