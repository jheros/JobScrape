"""Probe a posting's own page to find out whether it is still live.

Some jobs are unreachable by the fetch sweep: a Hacker News "Who is hiring?"
comment is never re-listed, and an aggregator we sample rather than enumerate
can never be a basis for closing what it did not return (see
:class:`~jobscrape.sources.base.JobBatch`). Those postings would stay open
forever. This closes them by asking the employer's own page.

**A posting is closed only on positive evidence of death.** Three verdicts, not
two, and the distinction is the whole design:

* ``EXPIRED``   — a definite death signal. Adds a strike.
* ``LIVE``      — a healthy page with no death signal. Clears the strikes.
* ``UNCERTAIN`` — 5xx, 403, a timeout, a blocked address. Changes **nothing**.

A 503 is not evidence a posting is alive, so it must not clear an accumulated
strike; it is not evidence it is dead either, so it must not add one. Collapsing
that into a boolean is how a site having a bad afternoon closes a catalogue.

Two consecutive strikes close a posting, so one transient death signal (a
mid-deploy page, a CDN error page) is absorbed. Nothing here ever *reopens* a
posting — a re-fetch does that through ``upsert_jobs``, which clears
``closed_at``. Without a browser we under-detect closures rather than risk
closing a live posting, and that bias is deliberate.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .safehttp import BlockedAddress, check_url

log = logging.getLogger("jobscrape.liveness")

EXPIRED = "expired"
LIVE = "live"
UNCERTAIN = "uncertain"

#: Consecutive expired reads before a posting is closed.
CLOSE_THRESHOLD = 2

#: Below this many characters a 2xx body is a shell (nav + footer), not a posting.
MIN_CONTENT_CHARS = 300

#: Phrases that only appear on a posting that is gone. Multi-word and specific:
#: a bare "closed" or "filled" appears in ordinary descriptions.
_EXPIRED_BODY = re.compile(
    r"(?i)"
    r"job (is )?no longer available"
    r"|job[^.]{0,40}no longer open"
    r"|(position|role|vacancy) has been filled"
    r"|this (position|role|job) (is )?no longer"
    r"|this job (listing )?is closed"
    r"|this (job|position) (has )?expired"
    r"|job posting has expired"
    r"|no longer accepting applications"
    r"|applications?\s+(?:(?:have|are|is)\s+)?closed"
    r"|job (listing )?not found"
    r"|the (job|position|page) you.{0,20}looking for (is no longer|does\s?n.t exist)"
)

#: The URL resolved to a search/index page, so the specific posting is gone.
_LISTING_BODY = re.compile(r"(?i)\d+\s+jobs?\s+found|search for jobs page is loaded")

#: Some ATSes redirect a dead posting to a URL carrying an error marker.
_EXPIRED_URL = re.compile(r"(?i)[?&](error=true|jobnotfound)")

#: URLs that are a *container* for a posting rather than the posting's own page.
#:
#: A Hacker News "Who is hiring?" comment lives at a thread URL that stays up
#: forever whether or not the role was filled, so a probe can only ever answer
#: LIVE — it would burn a request to learn nothing and, worse, would clear
#: strikes accumulated from a genuine signal. Measured on the archive: 227 of
#: 536 open hackernews postings carry the thread URL and 309 carry a real
#: employer link, so this is filtered per-URL rather than per-source.
_UNPROBABLE_URL = re.compile(r"(?i)https?://(news\.ycombinator\.com|t\.me)/")


def is_probable(url: str | None) -> bool:
    """Can a probe of this URL tell us anything about the posting?"""
    return bool(url) and not _UNPROBABLE_URL.search(url)


@dataclass(frozen=True)
class Probe:
    verdict: str
    reason: str = ""


def classify(status: int, final_url: str, body: str) -> Probe:
    """Judge one fetched page, most definitive signal first."""
    if status in (404, 410):
        return Probe(EXPIRED, "http_gone")
    if status < 200 or status >= 300:
        # 5xx, 403, 429 — the site is unhappy, which says nothing about the job.
        return Probe(UNCERTAIN)
    if _EXPIRED_URL.search(final_url or ""):
        return Probe(EXPIRED, "expired_url")
    if _EXPIRED_BODY.search(body or ""):
        return Probe(EXPIRED, "expired_body")
    if _LISTING_BODY.search(body or ""):
        return Probe(EXPIRED, "listing_page")
    if len((body or "").strip()) < MIN_CONTENT_CHARS:
        return Probe(EXPIRED, "insufficient_content")
    return Probe(LIVE)


async def probe_url(fetcher, url: str) -> Probe:
    """Fetch ``url`` through the SSRF guard and classify it.

    A blocked address is ``UNCERTAIN``, not ``EXPIRED``: refusing to look at
    something is not evidence about it.
    """
    try:
        check_url(url)
    except BlockedAddress as exc:
        log.debug("liveness: refusing %s — %s", url, exc)
        return Probe(UNCERTAIN, "blocked")

    body = await fetcher.get_text(url, retries=1)
    if body is None:
        # The Fetcher collapses every failure to None, so this covers timeouts,
        # connection errors and any 4xx/5xx alike. None of them are evidence.
        return Probe(UNCERTAIN, "no_response")
    return classify(200, url, body)


def apply_verdict(strikes: int, verdict: str) -> tuple[int, bool]:
    """Fold a verdict into a strike count.

    Returns ``(new_strikes, should_close)``.
    """
    if verdict == EXPIRED:
        strikes += 1
        return strikes, strikes >= CLOSE_THRESHOLD
    if verdict == LIVE:
        return 0, False
    return strikes, False  # UNCERTAIN: untouched, in both directions
