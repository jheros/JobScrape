"""Indeed and LinkedIn postings, ingested from Apify actor runs.

Not a :class:`~jobscrape.sources.base.Source`, and deliberately so. Every other
adapter here is asked "what do you have?" on this project's schedule. Apify
inverts that: its scheduler runs the actors, and a webhook tells us a dataset is
ready. There is no search for this app to perform, so the fetch/probe contract
would be a lie.

What lives here instead is the mapping — dataset items in, :class:`Job` out —
as pure functions over lists of dicts. That is the part with real behavioural
risk, and it is testable with no network and no token.

The pipeline downstream is unchanged: `apply_filters_counted` decides what to
keep and `db.upsert_jobs` writes it, exactly as for a fetched source.

Two things worth knowing before reading further:

* **The webhook carries no jobs.** Apify's payload is run metadata — an
  `actorRunId` and a `defaultDatasetId`. The items must be fetched from the API
  with a token, so `client.py` exists no matter how ingestion is triggered.
* **These are aggregators**, so they duplicate postings the ATS boards already
  carry. `dedupe.py` suppresses an aggregator row whenever an ATS row describes
  the same role, but only when the two agree on `company_slug` — see
  :mod:`~jobscrape.sources.apify.ats_link` for how that agreement is arranged.
"""

from __future__ import annotations

import logging
from typing import Any

from ... import config
from . import ats_link, client, indeed, linkedin  # noqa: F401  (re-exported)

log = logging.getLogger("jobscrape.apify")

#: Keyword found in an actor's *name* -> the module that maps its output.
MAPPERS = {
    "indeed": indeed,
    "linkedin": linkedin,
}

#: Resolved actor id -> mapper, so an id is looked up over the network once per
#: process rather than once per run.
_RESOLVED: dict[str, Any] = {}


def mapper_for(actor: str | None):
    """Pick a mapper from an actor's readable NAME, or ``None``.

    Matches `cheap_scraper~indeed-job-scraper` and `indeed-job-scraper`. It
    cannot match an opaque id — use :func:`resolve_mapper` for that, which is
    what every real code path needs.
    """
    name = (actor or "").lower()
    for key, module in MAPPERS.items():
        if key in name:
            return module
    return None


async def resolve_mapper(fetcher, actor: str | None):
    """Pick a mapper for whatever a *run* reports as its actor.

    A run's `actId` is an **opaque id** — `nlZZi3lZre4fM9IET`, not
    `cheap_scraper~indeed-job-scraper`. `mapper_for` matches on the name
    containing "indeed" or "linkedin", so it returns ``None`` for every real
    run, and ingestion failed with "no mapper for actor" every time. The two
    fallbacks originally written beside it (`actorTaskId`, `buildId`) are opaque
    ids too, so they could not have helped.

    Three steps, cheapest first:

    1. **The configured values.** `APIFY_INDEED_ACTOR` and `APIFY_LINKEDIN_ACTOR`
       may hold either form, so a direct comparison resolves the common case
       with no request.
    2. **The name, if this already is one.** Covers `user~actor-name` and tests.
    3. **Ask Apify.** `GET /v2/acts/{id}` is public for a public actor and
       returns the readable `name`. Cached per process.

    ``None`` when nothing matches — the honest answer, since guessing a schema
    would write another actor's fields into these columns.
    """
    raw = (actor or "").strip()
    if not raw:
        return None
    if raw in _RESOLVED:
        return _RESOLVED[raw]

    configured = (
        (config.APIFY_INDEED_ACTOR, indeed),
        (config.APIFY_LINKEDIN_ACTOR, linkedin),
    )
    for value, module in configured:
        if value and raw.lower() == value.strip().lower():
            _RESOLVED[raw] = module
            return module

    module = mapper_for(raw)
    if module is not None:
        _RESOLVED[raw] = module
        return module

    # A lookup failure is not a reason to abort ingestion — it is a reason to
    # report "no mapper", the same as an unknown actor. Anything raised here
    # (404 on a private actor, a timeout, no fetcher at all) would otherwise
    # propagate out of `ingest_run` as a traceback instead of a message.
    try:
        name = await client.actor_name(fetcher, raw)
    except Exception as exc:                      # noqa: BLE001 - reported, not raised
        # Deliberately NOT cached: a timeout must not pin this actor to "no
        # mapper" for the life of the process.
        log.warning("apify: could not look up actor %s: %s", raw, exc)
        return None
    module = mapper_for(name)
    _RESOLVED[raw] = module
    return module


# Imported LAST, and that ordering is load-bearing: `ingest` calls
# `mapper_for`, so it cannot be imported before this module has defined it.
#
# It was previously loaded lazily through a module `__getattr__` instead, to
# keep `db` and `filters` out of a plain mapper import. That was wrong twice
# over. `from . import ingest` inside `__getattr__` re-enters `__getattr__` —
# `from X import Y` resolves Y with `getattr(X, "Y")` — so every real ingestion
# raised RecursionError while the tests passed, because they stub it. And there
# was no cycle to avoid in the first place: `db` and `filters` import `models`
# and `config`, neither of which imports `sources`.
from . import ingest  # noqa: E402,F401  (must follow `mapper_for`)

__all__ = [
    "MAPPERS", "ats_link", "client", "indeed", "ingest", "linkedin",
    "mapper_for", "resolve_mapper",
]
