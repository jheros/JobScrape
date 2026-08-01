"""Apify webhook receiver — one route, deliberately.

Separate from the dashboard app, and that separation is the point. The
dashboard has **no authentication on any route**: `/api/runs/{stage}/start` and
`/stop` launch and kill pipeline stages, `/api/jobs/bulk` rewrites job
statuses. It is safe only because it binds to 127.0.0.1. This receiver is the
one thing that has to be reachable from the internet, so it shares nothing with
the dashboard but the database.

What Apify actually sends
=========================

Not the jobs. The payload is run metadata::

    {"eventType": "ACTOR.RUN.SUCCEEDED",
     "eventData": {"actorId": "...", "actorRunId": "..."},
     "resource": {"id": "...", "status": "SUCCEEDED", "defaultDatasetId": "..."}}

The postings live in a dataset that has to be fetched with an API token
afterwards, so this route is a trigger rather than a sink.

Three constraints from Apify's documented behaviour shape the handler:

1. **It retries 11 times over ~32 hours** on any non-2xx. Fetching up to 1,000
   jobs, filtering and upserting will not finish inside a normal HTTP timeout,
   and a slow response means a second delivery starts while the first is still
   running. So the route acknowledges *before* doing the work.
2. **There is no request signing or HMAC.** A shared secret is the only
   authentication available; Apify's own docs recommend putting one in the URL.
   Both a header and a URL segment are accepted here.
3. **A payload is therefore untrusted input.** It names both a run and a
   dataset, so the dataset id is re-read from the run via the API rather than
   taken from the body — otherwise a forged request could point ingestion at
   any dataset on the account.

Re-delivery needs no special handling: `db.upsert_jobs` is keyed on
``(source, source_job_id)`` with ``ON CONFLICT DO UPDATE``, so processing the
same run twice converges instead of duplicating.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from . import config, db

log = logging.getLogger("jobscrape.webhook")

#: The only event worth acting on. Apify can be configured to call on failure
#: or timeout too, and those carry a partial dataset at best.
SUCCEEDED = "ACTOR.RUN.SUCCEEDED"

app = FastAPI(title="jobscrape apify webhook", docs_url=None, redoc_url=None)


def _secret_ok(presented: str | None) -> bool:
    """Constant-time comparison against the configured secret.

    An unset secret rejects everything rather than allowing everything: this
    route is meant to be public, and the failure mode of the opposite default
    is an open ingestion endpoint.
    """
    expected = config.APIFY_WEBHOOK_SECRET
    if not expected:
        return False
    return bool(presented) and hmac.compare_digest(str(presented), expected)


def _run_id(payload: dict[str, Any]) -> str | None:
    """The run id, from either place Apify puts it."""
    event = payload.get("eventData")
    if isinstance(event, dict) and event.get("actorRunId"):
        return str(event["actorRunId"])
    resource = payload.get("resource")
    if isinstance(resource, dict) and resource.get("id"):
        return str(resource["id"])
    return None


async def _ingest(run_id: str) -> None:
    """Background half: fetch, map, filter, store.

    Its own connection — the request's has been returned by the time this runs.
    Exceptions are logged rather than raised: nothing is waiting on them, and
    `ingest_and_record` has already written the failure to `runs`.
    """
    from .sources.apify import ingest as ingest_module

    with db.session() as conn:
        await ingest_module.ingest_and_record(conn, run_id)


@app.get("/health")
async def health() -> JSONResponse:
    """Liveness only — deliberately says nothing about the archive.

    This endpoint is public. The dashboard's `/health` reports row counts, disk
    headroom and the age of the last run, which is exactly the sort of detail
    an unauthenticated caller should not be handed.
    """
    return JSONResponse({"ok": True})


@app.post("/apify/{secret}")
@app.post("/apify")
async def apify(
    request: Request,
    background: BackgroundTasks,
    secret: str | None = None,
    x_apify_secret: str | None = Header(default=None),
) -> JSONResponse:
    """Accept a run-finished notification and ingest it out of band.

    The secret may arrive as the `X-Apify-Secret` header (Apify's headers
    template) or as the last path segment, since a URL token is the only option
    when headers are awkward to configure.
    """
    if not _secret_ok(x_apify_secret or secret):
        log.warning("apify webhook: rejected a request with a bad or missing secret")
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        payload = await request.json()
    except Exception:                              # noqa: BLE001 - malformed body
        return JSONResponse({"error": "body is not JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "body is not an object"}, status_code=400)

    event = str(payload.get("eventType") or "")
    if event != SUCCEEDED:
        # 200 on purpose. This is a delivery we understand and are declining to
        # act on; a non-2xx would make Apify retry it ten more times.
        log.info("apify webhook: ignoring %s", event or "an event with no type")
        return JSONResponse({"ok": True, "ignored": event})

    run_id = _run_id(payload)
    if not run_id:
        return JSONResponse({"error": "no actorRunId in payload"}, status_code=400)

    # Acknowledged here, before any work — see constraint 1 in the module
    # docstring. The dataset id in the payload is deliberately not passed on;
    # `ingest_run` re-reads it from the run.
    background.add_task(_ingest, run_id)
    log.info("apify webhook: accepted run %s", run_id)
    return JSONResponse({"ok": True, "run_id": run_id, "status": "accepted"})


def serve(host: str = "0.0.0.0", port: int = 8001) -> None:
    """Run the receiver.

    Defaults to all interfaces, unlike the dashboard's 127.0.0.1 — this one is
    meant to be reachable. It refuses to start without a secret rather than
    coming up as an open endpoint.
    """
    import uvicorn

    if not config.APIFY_WEBHOOK_SECRET:
        raise SystemExit(
            "APIFY_WEBHOOK_SECRET is not set. The receiver is intended to be "
            "publicly reachable and a shared secret is the only authentication "
            "Apify supports, so it will not start without one."
        )
    if not config.APIFY_TOKEN:
        raise SystemExit(
            "APIFY_TOKEN is not set. Apify's webhook carries only a run id, so "
            "the token is required to fetch the dataset it points at."
        )
    config.ensure_dirs()
    uvicorn.run(app, host=host, port=port, log_level="info")
