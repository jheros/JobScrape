"""Is this posting a real, current opening?

A role that has been up for eight months, that the employer lists fifteen
concurrent copies of, or that literally says "talent pool" is not an opening you
can apply to today. This module scores that from evidence the archive already
holds, so the daily queue can be sorted by whether a posting is worth the time.

**Nothing here is stored.** The verdict is time-dependent — a posting that is
`fresh` today is `stale` in three months with no data having changed — so it is
computed on read from `(posted_at, cluster counts, description)` plus `now`.
Storing it would mean a column that is silently wrong between backfills, and a
threshold change would need a migration; computed on read, a threshold change
takes effect on the next page load.

:func:`classify` is a pure function over scalars: no database, no clock of its
own (`now` is a parameter). Adapted from freehire's `internal/jobreality`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

#: Age at or over which the "old" signal fires.
OLD_AGE_DAYS = 90
#: Age at or under which a posting with no evergreen signal counts as fresh.
FRESH_WINDOW_DAYS = 14
#: Concurrent open postings of one role that count as mass-posting.
MASS_THRESHOLD = 5
#: Historical churn (reposts beyond the concurrent ones) that counts as reposting.
REPOST_THRESHOLD = 3
#: Independent signals required before a posting is called likely-evergreen.
CONVERGENCE = 2

FRESH = "fresh"
STALE = "stale"
LIKELY_EVERGREEN = "likely_evergreen"

#: Phrases that mark a posting as a standing advertisement rather than a role.
#:
#: Deliberately **multi-word**. A bare word like "pipeline" would fire on every
#: "data pipeline" in the catalogue; the dictionary never guesses, so each entry
#: is a phrase that has no innocent reading in a job description.
EVERGREEN_PHRASES = (
    "always hiring",
    "always looking for",
    "we are always",
    "we're always",
    "talent community",
    "talent pool",
    "talent network",
    "talent pipeline",
    "future opportunities",
    "future openings",
    "general application",
    "open application",
    "speculative application",
    "ongoing recruitment",
    "evergreen requisition",
)


@dataclass(frozen=True)
class Staleness:
    """The verdict plus the evidence behind it, so a UI can explain itself."""

    verdict: str
    age_days: int | None
    old: bool
    mass_posted: bool
    reposted: bool
    evergreen_text: bool

    @property
    def signals(self) -> int:
        return sum((self.old, self.mass_posted, self.reposted, self.evergreen_text))

    @property
    def reasons(self) -> list[str]:
        out = []
        if self.old and self.age_days is not None:
            out.append(f"posted {self.age_days} days ago")
        if self.mass_posted:
            out.append("many concurrent copies of this role")
        if self.reposted:
            out.append("repeatedly reposted")
        if self.evergreen_text:
            out.append("worded as a standing advertisement")
        return out


def has_evergreen_text(description: str | None) -> bool:
    if not description:
        return False
    lowered = description.lower()
    return any(phrase in lowered for phrase in EVERGREEN_PHRASES)


def _age_days(posted_at: datetime | str | None, now: datetime) -> int | None:
    if not posted_at:
        return None
    if isinstance(posted_at, str):
        try:
            posted_at = datetime.fromisoformat(posted_at)
        except ValueError:
            return None
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    return max((now - posted_at) // timedelta(days=1), 0)


def classify(
    *,
    posted_at: datetime | str | None,
    repost_count: int = 1,
    mass_count: int = 1,
    description: str | None = None,
    now: datetime | None = None,
) -> Staleness:
    """Score one posting.

    ``repost_count`` counts every stored posting sharing this role's
    ``(company_slug, role_fingerprint)``, any status; ``mass_count`` counts only
    the currently-open ones.

    **Age is measured from ``posted_at``, the employer's own date** — a
    deliberate divergence from freehire, which measures from first crawl. That
    works for a years-old catalogue; ours is days old, so a first-crawl age
    reads 0 for every row and the signal would be dead for months. ``posted_at``
    is populated on ~100% of our open jobs and answers the question the signal
    is actually asking.
    """
    now = now or datetime.now(timezone.utc)
    age = _age_days(posted_at, now)

    old = age is not None and age >= OLD_AGE_DAYS
    mass_posted = mass_count >= MASS_THRESHOLD
    # Reposting and mass-posting must be INDEPENDENT signals. mass_count (open)
    # is a subset of repost_count (any status), so counting both raw would let
    # a single concurrent spray fire two signals and reach the verdict on its
    # own. This counts only the HISTORICAL churn — copies beyond the concurrent
    # ones, i.e. roles closed and posted again under new ids over time.
    reposted = (repost_count - mass_count) >= REPOST_THRESHOLD
    evergreen = has_evergreen_text(description)

    signals = sum((old, mass_posted, reposted, evergreen))
    if signals >= CONVERGENCE:
        verdict = LIKELY_EVERGREEN
    elif age is not None and age <= FRESH_WINDOW_DAYS and not evergreen:
        verdict = FRESH
    else:
        verdict = STALE

    return Staleness(
        verdict=verdict, age_days=age, old=old,
        mass_posted=mass_posted, reposted=reposted, evergreen_text=evergreen,
    )


# --- the same rule, expressed in SQL ----------------------------------------
#
# Filtering by verdict has to happen in the database, or pagination would be
# computed over a page that was already truncated. That means the rule exists
# twice, which is a drift risk — so both forms are BUILT FROM THE CONSTANTS
# ABOVE, and `tests/test_staleness.py` asserts the two agree row-for-row
# against the real archive. Change a threshold in one place and both follow.


#: The phrase list as ILIKE patterns, for a one-off backfill scan.
EVERGREEN_PATTERNS = [f"%{phrase}%" for phrase in EVERGREEN_PHRASES]


def sql_signal_count(
    posted_at: str = "j.posted_at",
    repost_count: str = "cl.repost_count",
    mass_count: str = "cl.mass_count",
    evergreen: str = "j.evergreen_text",
) -> str:
    """SQL that evaluates to the number of staleness signals firing."""
    return (
        f"((({posted_at} IS NOT NULL AND {posted_at} <= now() - interval "
        f"'{OLD_AGE_DAYS} days'))::int"
        f" + (COALESCE({mass_count}, 1) >= {MASS_THRESHOLD})::int"
        f" + ((COALESCE({repost_count}, 1) - COALESCE({mass_count}, 1))"
        f"    >= {REPOST_THRESHOLD})::int"
        f" + COALESCE({evergreen}, false)::int)"
    )


def sql_verdict(
    posted_at: str = "j.posted_at",
    repost_count: str = "cl.repost_count",
    mass_count: str = "cl.mass_count",
    evergreen: str = "j.evergreen_text",
) -> str:
    """SQL that evaluates to the verdict string. Takes no bound parameters.

    Reads the **stored** ``evergreen_text`` column rather than scanning
    descriptions. That is not a compromise of the compute-on-read rule: the
    rule is that *verdicts* are not stored because they are time-dependent.
    Whether a description contains "talent pool" is not time-dependent — it is
    evidence, fixed at the moment the description was captured, and it changes
    only when the description does.

    Measured before this changed: matching the 15 phrases with ``ILIKE ANY``
    across the open catalogue took **52 seconds** for one query. Leading
    wildcards cannot use an index, so it was 15 full substring scans over
    multi-kilobyte descriptions for every row, on every page load.
    """
    signals = sql_signal_count(posted_at, repost_count, mass_count, evergreen)
    return (
        f"CASE WHEN {signals} >= {CONVERGENCE} THEN '{LIKELY_EVERGREEN}'"
        f" WHEN {posted_at} IS NOT NULL"
        f"      AND {posted_at} >= now() - interval '{FRESH_WINDOW_DAYS} days'"
        f"      AND NOT COALESCE({evergreen}, false) THEN '{FRESH}'"
        f" ELSE '{STALE}' END"
    )


#: The join every staleness query needs: per-role repost/concurrent counts.
CLUSTER_JOIN = """
LEFT JOIN (
    SELECT company_slug, role_fingerprint,
           COUNT(*) AS repost_count,
           COUNT(*) FILTER (WHERE closed_at IS NULL) AS mass_count
    FROM jobs
    WHERE role_fingerprint IS NOT NULL AND role_fingerprint <> ''
    GROUP BY company_slug, role_fingerprint
) cl ON cl.company_slug = j.company_slug
    AND cl.role_fingerprint = j.role_fingerprint
"""
