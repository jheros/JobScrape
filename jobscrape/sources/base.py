"""Adapter interfaces shared by every job source."""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol

from ..http import Fetcher
from ..models import Job


class JobBatch(list):
    """A source's postings, plus whether the crawl that produced them finished.

    A ``list`` subclass rather than a wrapper so every existing ``len(jobs)`` /
    ``for j in jobs`` call site keeps working untouched — the extra bit is
    provenance about the *crawl*, not about any one job.

    ``complete`` is what makes source-wide closure safe. Every global adapter
    paginates, and every one of them stops early and returns what it has when a
    page comes back malformed — so a rate-limited run is indistinguishable from
    a genuinely shrunken catalogue unless the adapter says so. Closing against
    a truncated result set retires postings the run simply never reached.

    The default is ``False``, and a **plain list carries no attribute at all**,
    so a caller reading it with ``getattr(jobs, "complete", False)`` treats
    anything that did not explicitly claim completeness as incomplete. Forgetting
    to set it therefore under-closes, which is the accepted direction; the
    reverse would silently delete live jobs.
    """

    def __init__(self, jobs: Any = (), *, complete: bool = False) -> None:
        super().__init__(jobs)
        self.complete = complete


def is_complete(jobs: Any) -> bool:
    """Did the crawl that produced ``jobs`` finish? Unknown counts as no."""
    return bool(getattr(jobs, "complete", False))


class Prefilter(Protocol):
    """Cheap "is this worth a detail request?" test.

    Sources that need a second request per posting (Ashby, SmartRecruiters,
    Workday) call this against the fields already present in the listing.
    Skipping non-matching postings avoids the large majority of those requests.
    Must be permissive: it decides only whether to *look closer*, never whether
    a job is finally kept — that stays with :mod:`jobscrape.filters`.
    """

    def __call__(
        self,
        title: str | None,
        department: str | None = None,
        location: str | None = None,
        country: str | None = None,
    ) -> bool: ...

# Search terms used by the query-based (non-ATS) sources.
#
# "qa engineer" was removed deliberately: QA roles are excluded by the role
# filter, and leaving it here meant Jooble, JSearch, Adzuna, USAJobs and Indeed
# actively spent request budget searching for postings that would then be
# thrown away. Order matters — Workday takes `[:4]` and LinkedIn `[:6]`, so
# reordering or removing an earlier entry silently changes what they search
# for. This one was last, which is why it was safe to drop.
DEFAULT_QUERIES = [
    "software engineer",
    "software developer",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "data engineer",
    "machine learning engineer",
    "devops engineer",
    "site reliability engineer",
    "security engineer",
    "mobile engineer",
]


#: Fields a shallow refresh must leave alone so the stored value survives.
#: These are exactly the columns `db.upsert_jobs` COALESCEs, and the two lists
#: have to agree — if an adapter sets one of these to None on a shallow pass and
#: the upsert does *not* preserve it, the stored value is destroyed.
PRESERVED_ON_REFRESH = frozenset({
    "description_html", "description_text", "posted_at",
    "salary_min", "salary_max", "salary_currency", "raw",
})


def shallow_refresh(job: Job) -> Job:
    """Strip the fields that only a detail request can supply.

    Used when a posting is already stored: we still want to record that it is
    still listed (so ``last_seen_at`` advances and it isn't marked closed), but
    re-downloading its description would be wasted work.

    Safe because `db.upsert_jobs` preserves these columns against NULL. The
    listing-derived fields — title, company, location, department — are left in
    place so genuine edits to them are still picked up.
    """
    job.description_html = None
    job.description_text = None
    job.posted_at = None
    job.salary_min = None
    job.salary_max = None
    job.salary_currency = None
    job.raw = {}
    return job


class Source:
    """Common metadata for all adapters."""

    name: str = "base"
    #: Human-readable note surfaced by ``jobscrape sources``.
    note: str = ""
    #: Whether the adapter needs credentials that may be absent.
    requires_key: bool = False

    def available(self) -> tuple[bool, str]:
        """Return ``(usable, reason)``. Missing keys are a skip, not an error."""
        return True, ""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"


class BoardSource(Source):
    """A per-company ATS board, addressed by a discovered slug."""

    ats: str = "base"

    async def probe(self, fetcher: Fetcher, slug: str) -> tuple[str, int]:
        """Cheaply classify a slug as ``live`` / ``empty`` / ``dead``.

        Used by the validate stage to discard the junk that the Common Crawl
        index inevitably contains.
        """
        jobs = await self.fetch_board(fetcher, slug, with_details=False)
        if jobs is None:
            return "dead", 0
        return ("live", len(jobs)) if jobs else ("empty", 0)

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        known_ids: set[str] | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        """Return all open postings for one company, or ``None`` if the board
        does not exist. An empty list means the board exists but is not hiring.

        ``prefilter``, when given, lets two-request sources skip detail lookups
        for postings that clearly will not survive filtering.

        ``known_ids`` holds ``source_job_id`` values already stored for this
        board. Sources that need a detail request per posting skip it for those
        and emit a *shallow* Job built from listing fields alone — see
        :func:`shallow_refresh`. This is what makes a repeat sweep cheap: after
        the first run almost every posting is already known, so only genuinely
        new ones cost a detail request.
        """
        raise NotImplementedError


class GlobalSource(Source):
    """A search-style API covering many employers at once."""

    async def fetch(
        self,
        fetcher: Fetcher,
        since: date | None = None,
        until: date | None = None,
        queries: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[Job]:
        """Return the postings this source currently lists.

        Should return a :class:`JobBatch` with ``complete=True`` when the crawl
        walked its whole result set, and ``complete=False`` when it stopped
        early (a bad page, a cap, an exhausted request budget). A bare list is
        read as incomplete — see :class:`JobBatch`.
        """
        raise NotImplementedError
