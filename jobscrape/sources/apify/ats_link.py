"""Recover a company's ATS board slug from an aggregator's apply link.

Why this exists
===============

Indeed and LinkedIn republish postings the ATS boards here already carry, so
almost every row they produce is a duplicate of one already in the archive.
`dedupe.py` handles that correctly in principle — tier 2 picks the canonical
**only from ATS rows**, so an aggregator can never outrank a company's own
board — but it clusters on ``(company_slug, role_key)``, and the two sides
derive that slug from different things:

===========  ====================  =========================
Source       company_slug from     Example
===========  ====================  =========================
ATS board    the board slug        ``10alabs``
aggregator   the company *name*    ``10a-labs``
===========  ====================  =========================

Those never match, and the cost is measurable on the archive as it stands:
Adzuna leaves 57% of its rows unsuppressed and Jooble 85%, which is duplicate
listings in front of a reader.

Name normalisation can only ever guess at that gap. An apply URL does not have
to: Indeed and LinkedIn overwhelmingly link *at* the employer's real board, so
the slug is sitting in the URL and can be read out exactly.

    https://boards.greenhouse.io/acme/jobs/4021  ->  acme
    https://jobs.lever.co/acme/9f2c…             ->  acme
    https://acme.wd1.myworkdayjobs.com/…         ->  acme

That is an exact match against the ATS row, not a similarity score.

When the link is not a board this project knows — a company's own careers page,
an Indeed-hosted apply flow — the answer is ``None`` and the caller falls back
to the company name. Guessing a slug would be worse than not having one: a
wrong slug clusters two unrelated companies' postings together.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

#: Boards addressed as ``host/slug/...``. The slug is the first path segment.
#:
#: Host suffixes rather than exact hosts, since several of these serve both a
#: bare and a ``www.`` form, and Greenhouse serves both ``boards.greenhouse.io``
#: and ``job-boards.greenhouse.io``.
_PATH_SLUG_HOSTS: tuple[tuple[str, str], ...] = (
    ("greenhouse.io", "greenhouse"),
    ("lever.co", "lever"),
    ("ashbyhq.com", "ashby"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("recruitee.com", "recruitee"),
    ("workable.com", "workable"),
    ("jobvite.com", "jobvite"),
    ("icims.com", "icims"),
)

#: Boards addressed as ``slug.host/...``. The slug is the first host label.
#:
#: Workday is the awkward one: the host is ``{tenant}.{wd}.myworkdayjobs.com``
#: where ``{wd}`` is a datacentre label (``wd1``, ``wd5``, ``wd103``), so the
#: tenant is the first label and the second must be discarded rather than
#: mistaken for it.
_HOST_SLUG_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("myworkdayjobs.com", "workday"),
    ("bamboohr.com", "bamboohr"),
    ("breezy.hr", "breezy"),
    ("applytojob.com", "jazzhr"),
    ("teamtailor.com", "teamtailor"),
    ("pinpointhq.com", "pinpoint"),
    ("rippling.com", "rippling"),
    ("csod.com", "cornerstone"),
    ("paycomonline.net", "paycom"),
)

#: Host labels that are never a tenant, so a URL beginning with one yields
#: nothing rather than a slug like "www" or "jobs".
_NOT_A_SLUG = frozenset({
    "www", "jobs", "job", "careers", "career", "apply", "boards", "board",
    "api", "app", "my", "secure", "recruiting", "hire", "talent",
})

_SLUG_CLEAN = re.compile(r"[^a-z0-9]+")


def board_slug(url: str | None) -> tuple[str, str] | None:
    """``(ats, slug)`` for an apply URL that points at a known board.

    Returns ``None`` for anything unrecognised, which is the common and
    acceptable case — a company's own careers page carries no slug this project
    could match against.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None

    for suffix, ats in _HOST_SLUG_SUFFIXES:
        if host.endswith(suffix):
            label = host[: -len(suffix)].strip(".").split(".")[0]
            if label and label not in _NOT_A_SLUG:
                return ats, label
            return None

    for suffix, ats in _PATH_SLUG_HOSTS:
        if host.endswith(suffix):
            parts = [p for p in (parsed.path or "").split("/") if p]
            # Greenhouse embeds an "embed/job_board?for=slug" form as well as
            # the plain path one; the query carries the slug there.
            for part in parts:
                if part.lower() in _NOT_A_SLUG or part.lower() in (
                    "embed", "job_board", "jobs", "postings"
                ):
                    continue
                return ats, part
            return None
    return None


def normalise_slug(value: str | None) -> str | None:
    """Strip a slug to its comparable core: lowercase alphanumerics only.

    ``10a-labs`` and ``10alabs`` are the same company written two ways, and
    that difference alone is why cross-source dedup was missing them. Applied
    to both sides of a comparison, never stored — the stored slug stays the one
    its source published, so a board URL built from it still resolves.
    """
    if not value:
        return None
    cleaned = _SLUG_CLEAN.sub("", str(value).lower())
    return cleaned or None
