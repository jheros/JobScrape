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

from . import ats_link, client, indeed, linkedin  # noqa: F401  (re-exported)

#: Actor id -> the module that maps its output. Keyed on the *slug* portion
#: (`indeed-job-scraper`) rather than the full `user~actor` string, because the
#: webhook payload reports a numeric actor id and the configured value may be
#: either form; `mapper_for` reconciles them.
MAPPERS = {
    "indeed": indeed,
    "linkedin": linkedin,
}


def mapper_for(actor: str | None):
    """Pick a mapper from whatever Apify called the actor, or ``None``.

    The payload's `actorId` is an opaque id, while the configured value is
    usually `user~actor-name`, so neither can be compared directly. Matching on
    the substring is crude but total: an actor whose name contains neither word
    is not one this app knows how to read, and returning ``None`` is the honest
    answer rather than guessing a schema.
    """
    name = (actor or "").lower()
    for key, module in MAPPERS.items():
        if key in name:
            return module
    return None


__all__ = ["MAPPERS", "ats_link", "client", "indeed", "linkedin", "mapper_for"]


def __getattr__(name: str):
    """`ingest` imported lazily.

    It pulls in `db` and `filters`, which drag in SQLAlchemy and the whole
    model layer. The mappers and `ats_link` are pure and should stay importable
    — and testable — without any of that.
    """
    if name == "ingest":
        from . import ingest as module

        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
