"""Normalized job record shared by every source adapter."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from html import unescape
from typing import Any

from .seniority import classify as classify_seniority

#: Fractional seconds in a timestamp, however many digits the source emits.
_FRACTIONAL_RE = re.compile(r"\.(\d{1,9})")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")
_BLOCK_RE = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|table|section)[^>]*>", re.I)


def html_to_text(html: str | None) -> str:
    """Flatten an HTML job description to readable plain text.

    Sources hand back wildly different markup; a couple of them double-encode
    entities, so we unescape twice before stripping tags.
    """
    if not html:
        return ""
    text = unescape(unescape(html))
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = text.replace("\xa0", " ")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _NL_RE.sub("\n\n", text).strip()


# --- locations ------------------------------------------------------------

#: A trailing run of pipe-delimited numbers — latitude/longitude that some ATS
#: exports append to the location string, e.g.
#: ``"Gaston, SC 29053 | 33.8316 | -81.1126"``.
#:
#: The ``+`` on the outer group eats every such segment as one tail. A numeric
#: tail is *required*, so ``"Berlin | Germany"`` is left alone; ``-?`` and
#: ``(\.\d+)?`` cover negative longitudes and integer-only coordinates.
_COORD_TAIL_RE = re.compile(r"\s*(\|\s*-?\d+(\.\d+)?\s*)+$")


def clean_location(value: str | None) -> str | None:
    """Strip a trailing coordinate tail from a raw location string."""
    if not value:
        return value
    return _COORD_TAIL_RE.sub("", value).strip() or None


# --- company slugs --------------------------------------------------------

#: Trailing legal-entity suffixes, stripped so "Coinbase, Inc." and "Coinbase"
#: produce one slug. Only ever removed from the *end*, only as a whole token, and
#: only **once** — so this can merge two names identical apart from the suffix and
#: nothing else. "Acme Foods" and "Acme Systems" stay distinct.
#:
#: Words that are commonly part of a real trading name are deliberately absent
#: ("Group", "Holdings", "Labs", "Studio"): stripping those would merge "Foo" with
#: the unrelated "Foo Group", and a wrong merge hides a real job while a missed one
#: only leaves a duplicate.
_LEGAL_SUFFIX_RE = re.compile(
    r"[\s,]+(?:"
    r"inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|plc|"
    r"gmbh|ag|s\.?a|n\.?v|b\.?v|ab|oy|srl|s\.?r\.?l|pty|pte|k\.?k|sas|sarl|aps"
    r")\.?\s*$",
    re.I,
)
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str | None) -> str:
    """Turn a company display name into a stable, comparable slug.

    Aggregator APIs supply a display name but no board slug, so ``company_slug``
    arrives empty on every row from adzuna, jooble, hackernews and friends — 27%
    of the archive at the time of writing. Deduplication keys on the slug, and a
    blank leading component would let every company on those sources cluster
    together, so the slug is derived here instead.

    Accents are folded (``Zürich`` → ``zurich``) because the same employer is
    spelled both ways across sources.
    """
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.replace("&", " and ").lower().strip()
    folded = _LEGAL_SUFFIX_RE.sub("", folded)
    return _SLUG_STRIP_RE.sub("-", folded).strip("-")


# --- workplace type -------------------------------------------------------

_RE_REMOTE = re.compile(
    r"\b(remote|work\s+from\s+home|wfh|telecommut\w*|virtual|home[\s-]based|"
    r"fully\s+distributed|anywhere)\b",
    re.I,
)
_RE_HYBRID = re.compile(
    r"\b(hybrid|partially\s+remote|remote[\s-]?friendly|flex(ible)?\s+(work|onsite|office)|"
    r"\d\s*(days?|x)\s*(per\s+week\s+)?in[\s-]?(the\s+)?office|"
    r"\d\s*days?\s+onsite|office[\s-]based\s+with\s+remote)\b",
    re.I,
)
_RE_ONSITE = re.compile(
    r"\b(on[\s-]?site|onsite|in[\s-]?office|in[\s-]?person|office[\s-]?based|"
    r"no\s+remote|not\s+remote|100%\s+in\s+office)\b",
    re.I,
)


#: Words that, appearing ANYWHERE in a posting, mark it remote — but only for a
#: posting nothing else could classify. See the final sweep in
#: :func:`classify_workplace`.
#:
#: This is a deliberately permissive rule, chosen to maximise how many remote
#: roles surface, and it is applied literally with no negation or context guard.
#: Measured on 12,000 unclassified postings, it resolves 2,433 of them (20%),
#: and two costs were accepted knowingly:
#:
#: * ~24% of matches are negations. "This position is expected to be 100%
#:   onsite (no hybrid or remote options)" contains "remote" and so reads as
#:   remote.
#: * ``virtual`` contributes 333 of those 2,433 and is overwhelmingly "virtual
#:   interviews" — the hiring process, not the work arrangement. It is the
#:   single largest systematic error here.
#:
#: If the remote count ever looks inflated, deleting ``"virtual"`` from this
#: tuple is the first thing to try; it is one line and needs no other change.
REMOTE_MENTION = (
    "remote",
    "work from home",
    "wfh",
    "telecommute",
    "virtual",
    "home-based",
    "home based",
    "fully distributed",
    "work from anywhere",
    "remote-first",
)


def classify_workplace(
    text: str | None = None,
    is_remote: bool | None = None,
    hint: str | None = None,
    full_text: str | None = None,
) -> str:
    """Classify a posting as ``remote`` / ``hybrid`` / ``onsite`` / ``unknown``.

    ``hint`` is a source's own workplace field (Lever ``workplaceType``, Ashby
    ``workplaceType``, Workday ``remoteType``, …) and wins over free text, which
    is far noisier. ``is_remote`` is a structured boolean (SmartRecruiters
    ``location.remote``, Workable ``telecommuting``) and is trusted next.

    ``full_text`` is the *whole* posting — title, location and the complete
    description, not the truncated ``text`` signal. It is read only by the final
    sweep, which fires when every other tier has given up: any word in
    :data:`REMOTE_MENTION` occurring anywhere then makes the posting remote.

    That sweep runs **last on purpose**. A posting the ATS field, the structured
    flag, or the wording already classified keeps that answer, so an explicit
    hybrid or onsite verdict is never overwritten — hybrid postings routinely
    say "2 days remote", and letting the sweep win would erase the distinction.
    It only ever fills a blank.

    Purely descriptive — nothing is filtered on this value. "unknown" is the
    honest answer for a bare city with no workplace wording, and is common.
    """
    if hint:
        lowered = hint.strip().lower()
        if "hybrid" in lowered:
            return "hybrid"
        if "remote" in lowered or "telecommute" in lowered:
            return "remote"
        if "onsite" in lowered or "on-site" in lowered or "office" in lowered:
            return "onsite"

    if is_remote is True:
        return "remote"

    body = text or ""
    if not body:
        # An explicit False flag is still evidence, just weaker than wording.
        return "onsite" if is_remote is False else "unknown"

    # Hybrid first: "hybrid remote" and "remote-friendly" are hybrid, not remote.
    if _RE_HYBRID.search(body):
        return "hybrid"
    if _RE_ONSITE.search(body):
        return "onsite"
    if _RE_REMOTE.search(body):
        return "remote"

    # Last resort: nothing above could tell. Sweep the entire posting for any
    # mention of remote work and take it at face value.
    if full_text:
        lowered = full_text.lower()
        if any(word in lowered for word in REMOTE_MENTION):
            return "remote"

    return "onsite" if is_remote is False else "unknown"


# --- salary extraction ----------------------------------------------------

# Matches "$120,000 - $180,000", "$120K–$180K", "$55.00 to $70.00",
# "$211.4K – $290.6K".
#
# The fractional part is `\d{1,2}`, not `\d{2}`: requiring exactly two decimal
# places meant a single-decimal abbreviation like "$211.4K" matched nothing at
# all (the "." stopped the first number, then the "." wasn't a range dash), so
# the whole range was silently read as "no salary". Ashby publishes
# compensation in exactly that form — 429 rows in the archive carried a salary
# this dropped on the floor.
_SALARY_RANGE_RE = re.compile(
    r"\$\s?([\d][\d,]*(?:\.\d{1,2})?)\s?([kK])?\s*(?:-|–|—|to|through)\s*"
    r"\$?\s?([\d][\d,]*(?:\.\d{1,2})?)\s?([kK])?",
)
_SALARY_CONTEXT = re.compile(
    r"(salary|compensation|pay\s+range|base\s+pay|pay\s+scale|annual|hourly|"
    r"\bOTE\b|total\s+cash|expected\s+range|target\s+range)",
    re.I,
)
_HOURLY_HINT = re.compile(r"(per\s+hour|/\s?hour|hourly|/\s?hr|an\s+hour)", re.I)

#: Hours in a US full-time year (40 × 52). Hourly ranges are converted with it
#: so ``salary_min`` means one thing everywhere.
#:
#: Before this, an hourly posting stored its raw rate — 488 of 29,898 salaried
#: rows across 11 sources held a number like ``22.0`` in the same column as
#: ``220000.0``, with no ``salary_period`` to tell them apart. Every filter,
#: sort and export therefore compared hourly rates against annual ones, and the
#: hourly ones sorted to the bottom as if they paid nothing. The two adapters
#: added alongside this (Paycom, Indeed) are heavily hourly, so the share was
#: about to grow sharply.
#:
#: The conversion is an estimate — it assumes full-time — but a *consistent*
#: estimate beats a column that silently means two things. ``salary_raw`` keeps
#: the text as published, so what the employer actually said is never lost and
#: the conversion stays auditable.
HOURS_PER_YEAR = 2080.0

#: How many of each pay period fall in a year. 2,080 hours (40 × 52) and 260
#: days are the US full-time conventions. Keyed on the bare period word so the
#: many spellings the APIs use ("HOUR", "hourly", "1 HOUR", "Annually") all
#: reduce to one entry — see :func:`annualise`.
PERIODS_PER_YEAR = {
    "hour": HOURS_PER_YEAR,
    "day": 260.0,
    "week": 52.0,
    "biweek": 26.0,
    "semimonth": 24.0,
    "month": 12.0,
    "quarter": 4.0,
    "year": 1.0,
}

_PERIOD_ALIASES = {
    "hourly": "hour", "hr": "hour", "perhour": "hour",
    "daily": "day", "weekly": "week",
    "biweekly": "biweek", "fortnightly": "biweek", "fortnight": "biweek",
    "semimonthly": "semimonth",
    "monthly": "month", "quarterly": "quarter",
    "yearly": "year", "annual": "year", "annually": "year", "annum": "year",
}


def period_factor(period: Any) -> float | None:
    """How many of ``period`` make a year, or ``None`` if it isn't a period.

    Tolerant of the shapes these APIs actually send — ``"HOUR"``, ``"hourly"``,
    ``"1 YEAR"`` (Ashby), ``"per year"`` — because every provider spells it
    differently and a missed spelling silently means "no salary".

    Returns ``None`` rather than defaulting to 1.0 for anything unrecognised.
    Defaulting to annual is the dangerous direction: it files an hourly rate as
    a salary, which is a *wrong* number rather than a missing one.
    """
    if not isinstance(period, str):
        return None
    # "1 YEAR" -> "year"; "per hour" -> "hour"; strip digits and filler words.
    words = re.findall(r"[a-z]+", period.lower())
    for word in words:
        if word in ("per", "a", "an", "the", "1"):
            continue
        key = _PERIOD_ALIASES.get(word, word)
        if key in PERIODS_PER_YEAR:
            return PERIODS_PER_YEAR[key]
    return None


def annualise(amount: Any, period: Any) -> float | None:
    """Scale ``amount`` to a yearly figure, or ``None`` if it can't be trusted.

    ``None`` when the amount isn't a positive number or the period isn't
    recognised — see :func:`period_factor` for why an unknown period must not
    fall back to "annual".
    """
    factor = period_factor(period)
    if factor is None:
        return None
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    return value * factor if value > 0 else None


def _to_amount(number: str, k_suffix: str | None) -> float | None:
    try:
        value = float(number.replace(",", ""))
    except ValueError:
        return None
    if k_suffix:
        value *= 1000
    return value


def parse_salary(text: str | None) -> tuple[float | None, float | None, str | None, str | None]:
    """Extract a plausible pay range from prose.

    Returns ``(min, max, currency, matched_text)``, with **min/max always
    annualised** — an hourly range is multiplied by :data:`HOURS_PER_YEAR` so
    the two numbers mean the same thing as every other row's. ``matched_text``
    is the range exactly as it was written, so the original is still on record.

    Ranges are validated against realistic annual and hourly bands (the band
    checked depends on which one the surrounding words indicate) so that
    unrelated dollar figures — funding rounds, revenue targets, discounts — are
    not mistaken for compensation.

    Must be run on plain text, not HTML: entity-encoded dollar signs and dashes
    (``&#36;``, ``&mdash;``) and inline tags routinely break the match.
    """
    if not text:
        return None, None, None, None

    best: tuple[float, float, str] | None = None
    for match in _SALARY_RANGE_RE.finditer(text):
        low = _to_amount(match.group(1), match.group(2))
        high = _to_amount(match.group(3), match.group(4))
        if low is None or high is None or high < low:
            continue

        window = text[max(0, match.start() - 120) : match.end() + 120]
        hourly = _HOURLY_HINT.search(window) is not None
        if hourly:
            # Plausibility is judged on the rate as written, before scaling —
            # the bands describe what an hourly rate looks like.
            if not (7.0 <= low <= 500.0 and high <= 1000.0):
                continue
            low, high = low * HOURS_PER_YEAR, high * HOURS_PER_YEAR
        elif not (20_000 <= low <= 2_000_000 and high <= 5_000_000):
            continue
        # Prefer a range that sits near explicit compensation wording.
        if _SALARY_CONTEXT.search(window) or best is None:
            best = (low, high, match.group(0).strip())
            if _SALARY_CONTEXT.search(window):
                break

    if best is None:
        return None, None, None, None
    return best[0], best[1], "USD", best[2]


def to_utc_iso(value: Any) -> str | None:
    """Coerce the many date shapes these APIs emit into an ISO-8601 UTC string.

    Handles epoch seconds/milliseconds, ``YYYY-MM-DD``, and ISO timestamps with
    ``Z`` or numeric offsets.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc).isoformat()
    if isinstance(value, (int, float)):
        seconds = float(value)
        # Lever hands back epoch milliseconds; anything this large must be ms.
        if seconds > 1e11:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return to_utc_iso(int(text))
    cleaned = text.replace("Z", "+00:00")
    # Recruitee sends "2026-07-16 13:23:08 UTC"; a trailing zone name is not
    # ISO-8601 and would otherwise drop the date entirely.
    if cleaned.endswith(" UTC") or cleaned.endswith(" GMT"):
        cleaned = cleaned[:-4] + "+00:00"
    # fromisoformat accepts *exactly* 3 or 6 fractional digits, while real
    # sources emit anything from 1 to 9: Jooble sends 7 (".7530000") and
    # USAJobs sends 4 (".0000"). Normalize to 6 — truncating the long ones and
    # padding the short ones. Without this those sources parse as undated and
    # every one of their jobs is dropped by the date window.
    cleaned = _FRACTIONAL_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), cleaned)
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        for fmt in (
            "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y",
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
            "%d %b %Y", "%b %d, %Y", "%d/%m/%Y",
            "%a, %d %b %Y %H:%M:%S %z",
        ):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


@dataclass
class Job:
    """One posting, normalized across all sources."""

    source: str
    source_job_id: str
    title: str
    company: str
    job_url: str

    apply_url: str | None = None
    company_slug: str | None = None
    location_raw: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    is_remote: bool | None = None
    #: remote / hybrid / onsite / unknown — descriptive only, never filtered on.
    workplace_type: str | None = None
    #: A source's own workplace field, used to classify then discarded.
    workplace_hint: str | None = None
    #: intern / entry / mid / senior / staff / principal / lead / manager /
    #: director / executive / unknown. Derived from the title unless an adapter
    #: sets it — see `jobscrape.seniority`. Descriptive, never filtered on.
    seniority: str | None = None
    employment_type: str | None = None
    department: str | None = None
    posted_at: str | None = None
    updated_at: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_raw: str | None = None
    description_html: str | None = None
    description_text: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.title = (self.title or "").strip()
        self.company = (self.company or "").strip()
        self.source_job_id = str(self.source_job_id)
        self.location_raw = clean_location(self.location_raw)
        # Board adapters pass the real ATS board slug, which is authoritative and
        # must not be rewritten. Only the search APIs leave this empty.
        if not (self.company_slug or "").strip():
            self.company_slug = slugify(self.company) or None
        else:
            self.company_slug = self.company_slug.strip()
        if not self.description_text and self.description_html:
            self.description_text = html_to_text(self.description_html)
        self.posted_at = to_utc_iso(self.posted_at)
        self.updated_at = to_utc_iso(self.updated_at)
        # Most sources have no structured pay field, so fall back to scanning
        # the description. Runs on plain text and on the whole body — pay
        # ranges are usually near the end of a posting.
        if not self.workplace_type:
            # Title and location carry the signal reliably; the description is
            # included but truncated, since boilerplate deep in a posting
            # ("we support remote work across the company") misleads more than
            # it informs.
            signal = " ".join(
                part for part in (
                    self.title,
                    self.location_raw,
                    (self.description_text or "")[:1500],
                ) if part
            )
            self.workplace_type = classify_workplace(
                signal, self.is_remote, self.workplace_hint,
                # The whole posting, untruncated — read only by the final
                # remote sweep, which fires when nothing above could decide.
                full_text=" ".join(
                    part for part in (
                        self.title, self.location_raw, self.description_text,
                    ) if part
                ),
            )
        if self.is_remote is None and self.workplace_type == "remote":
            self.is_remote = True

        # Title only, deliberately — see `jobscrape.seniority`. An adapter whose
        # source states the level outright keeps its own value; this only fills
        # a blank, the same contract `workplace_type` has with `workplace_hint`.
        if not self.seniority:
            self.seniority = classify_seniority(self.title)

        if self.salary_min is None:
            haystack = self.salary_raw or self.description_text
            low, high, currency, matched = parse_salary(haystack)
            if low is not None:
                self.salary_min = low
                self.salary_max = high
                self.salary_currency = self.salary_currency or currency
                self.salary_raw = self.salary_raw or matched

    @property
    def posted_date(self) -> date | None:
        if not self.posted_at:
            return None
        try:
            return datetime.fromisoformat(self.posted_at).date()
        except ValueError:
            return None

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["raw_json"] = json.dumps(row.pop("raw"), default=str)[:400_000]
        # An input to classification, not a stored column.
        row.pop("workplace_hint", None)
        return row
