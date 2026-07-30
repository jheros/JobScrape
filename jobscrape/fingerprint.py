"""Content hashes used to recognise the same role posted more than once.

Three hashes, and confusing them is the classic way to get this wrong:

``role_fingerprint``
    The **identity** signal. Company + title + description, and deliberately
    *not* ``posted_at``, ``job_url``, ``source``, ``source_job_id`` or location.
    Excluding those is the entire point: a repost differs from the original in
    precisely the date and the URL, so any hash that includes them can never
    recognise one.

``role_key``
    A looser identity signal — company + title, no description. Needed because
    aggregators hard-truncate descriptions (adzuna caps at 500 characters,
    jooble at ~294) so their copy of a posting can never be byte-equal to the
    4,000-plus characters the employer's own board serves. Measured on the live
    archive, the strict fingerprint found **zero** cross-source clusters.

    Being looser, it over-merges if used carelessly: of 2,228 same-company
    same-title clusters on direct ATS boards, only 546 were true reposts and
    1,682 were genuinely distinct requisitions. So ``role_key`` is only ever
    consulted **across** sources, and only ever to suppress an aggregator copy in
    favour of the employer's own — see :mod:`jobscrape.dedupe`.

``content_hash``
    The **change** signal, which *includes* ``posted_at``. It answers "did this
    posting get edited since we last saw it?" and must **never** be used to
    cluster reposts.

The normalisation is deliberately shallow — no stemming, no punctuation
stripping, no stopword removal. Every one of those merges more aggressively, and
a wrong merge hides a real job while a missed one merely leaves a duplicate.
"""

from __future__ import annotations

import hashlib
import re
from html import unescape

from .models import Job

#: Field separator. A control character, so it cannot occur in real field text
#: and "ab" + "c" can never collide with "a" + "bc".
_SEP = "\x1e"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

#: One trailing location or qualifier clause: "Senior Engineer, Krakow",
#: "Backend Engineer - Remote", "Designer | Growth".
#:
#: A comma needs no leading space, but a dash, pipe or at-sign does — otherwise
#: "Front-End Engineer" would be cut at its own hyphen and "C++/Rust" mangled.
_TRAILING_CLAUSE_RE = re.compile(r"(?:\s*,\s*|\s+[|@]\s*|\s+[-–—]\s*)[^,|@\-–—]*$")

_PARENS_RE = re.compile(r"[()]")


def unwrap_parens(title: str | None) -> str:
    """Replace parentheses with spaces, keeping the words between them.

    Used only by :func:`role_key`, the cross-source key. ``Data Engineer (Semi
    Senior)`` and ``Data Engineer Semi Senior`` are one role written two ways —
    freehire measured that counting them separately inflated their cross-source
    absent-count by 13%.

    The contents are **unwrapped, not dropped**: dropping them would collapse
    ``Go Developer (Remote)`` onto ``Rust Developer (Remote)``, merging two
    unrelated roles into one.
    """
    if not title:
        return ""
    return _PARENS_RE.sub(" ", title)


def normalize_text(value: str | None) -> str:
    """Flatten text for comparison: tags out, entities decoded, case folded.

    Order matters twice over:

    * Tags are replaced with a **space**, not deleted — otherwise ``a</p><p>b``
      normalises to ``ab`` and two different descriptions can collide.
    * Entities are decoded **after** the tags are stripped, so a literal
      ``&lt;div&gt;`` shown to a reader is never mistaken for real markup.
    """
    if not value:
        return ""
    text = _TAG_RE.sub(" ", value)
    text = unescape(text)
    return _WS_RE.sub(" ", text).strip().lower()


def strip_trailing_clause(title: str | None) -> str:
    """Drop one trailing location/qualifier clause from a job title.

    ``Senior Engineer, Krakow`` and ``Senior Engineer, Berlin`` are one role
    advertised in two places, so they must share a key.

    The title is returned **unchanged if stripping would leave fewer than two
    words**. Without that guard a title like ``Engineer - Payments`` collapses to
    the single generic token ``Engineer``, which then becomes a cluster key broad
    enough to merge unrelated roles at the same company.
    """
    if not title:
        return ""
    text = title.strip()
    stripped = _TRAILING_CLAUSE_RE.sub("", text).strip()
    if len(stripped.split()) < 2:
        return text
    return stripped


def _digest(*parts: str) -> str:
    return hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()


def _description_of(job: Job) -> str:
    """Prefer the already-flattened text; fall back to raw markup.

    ``normalize_text`` strips tags either way, so both paths converge.
    """
    return job.description_text or job.description_html or ""


def role_fingerprint(job: Job) -> str:
    """Identity of a role: company + title + description. See module docstring.

    Returns ``""`` when there is nothing to key on, so callers can skip rows that
    would otherwise all cluster together under an empty hash.
    """
    slug = (job.company_slug or "").strip().lower()
    title = normalize_text(strip_trailing_clause(job.title))
    body = normalize_text(_description_of(job))
    if not slug or not title:
        return ""
    return _digest(slug, title, body)


def role_key(job: Job) -> str:
    """Looser identity: company + title only. Cross-source use only.

    Parentheses are unwrapped **before** the trailing clause is stripped, so a
    parenthesised trailing qualifier becomes ordinary strippable text — see
    :func:`unwrap_parens`.
    """
    slug = (job.company_slug or "").strip().lower()
    title = normalize_text(strip_trailing_clause(unwrap_parens(job.title)))
    if not slug or not title:
        return ""
    return _digest(slug, title)


def content_hash(job: Job) -> str:
    """Change signal — includes ``posted_at``. Never cluster on this."""
    return _digest(
        (job.company_slug or "").strip().lower(),
        normalize_text(job.title),
        normalize_text(_description_of(job)),
        job.posted_at or "",
        job.location_raw or "",
        job.apply_url or job.job_url or "",
        "" if job.salary_min is None else f"{job.salary_min:.2f}",
        "" if job.salary_max is None else f"{job.salary_max:.2f}",
    )
