"""Runtime configuration. API keys come from the environment or a .env file."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "jobs.db"
EXPORT_DIR = DATA_DIR / "exports"


def _load_dotenv() -> None:
    """Minimal .env reader so we don't need python-dotenv as a dependency."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Real environment variables win over the file.
        os.environ.setdefault(key, value)


_load_dotenv()


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, "") or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# --- Database -------------------------------------------------------------
# PostgreSQL. `psycopg2` is the driver: the storage layer is synchronous and is
# called under an asyncio lock, so an async driver would mean rewriting every
# call site for no present gain.
DATABASE_URL = _env_str(
    "DATABASE_URL",
    f"postgresql+psycopg2:///{os.environ.get('PGDATABASE') or 'jobscrape'}",
)
#: Used by the test suite. Kept separate so a stray test run can never truncate
#: the real archive — see `tests/conftest.py`, which refuses to run without it.
TEST_DATABASE_URL = _env_str(
    "JOBSCRAPE_TEST_DATABASE_URL", "postgresql+psycopg2:///jobscrape_test"
)
#: A wide sweep runs ~16 concurrent workers that all reach the same connection.
DB_POOL_SIZE = _env_int("JOBSCRAPE_DB_POOL_SIZE", 5)
DB_MAX_OVERFLOW = _env_int("JOBSCRAPE_DB_MAX_OVERFLOW", 10)
DB_POOL_TIMEOUT = _env_float("JOBSCRAPE_DB_POOL_TIMEOUT", 30.0)
DB_ECHO = os.environ.get("JOBSCRAPE_DB_ECHO", "") in ("1", "true", "yes")

#: How long a posting may be absent from a successful board fetch before it is
#: treated as closed. Closing on the first absence is wrong often enough to
#: matter: boards paginate inconsistently and truncate under load, and a posting
#: wrongly closed disappears from the queue while it is still hiring.
CLOSURE_GRACE_HOURS = _env_int("JOBSCRAPE_CLOSURE_GRACE_HOURS", 48)

#: Sources the fetch sweep can never close, so their postings need URL probing.
#:
#: These are the *sampling* sources: they read a bounded slice rather than
#: enumerating a catalogue, so absence from a run means nothing and they never
#: report a complete crawl (see `sources.base.JobBatch`). Board sources are
#: swept per board, and a global source that reports a complete crawl is swept
#: by source — neither belongs here.
LIVENESS_SOURCES = tuple(
    s for s in _env_str("JOBSCRAPE_LIVENESS_SOURCES", "hackernews,jsearch").split(",") if s
)

#: Employers whose postings are never collected, ADDED to the built-in list in
#: `filters.BLOCKED_COMPANIES`. Comma-separated names as you would write them —
#: "United Health Group" and "unitedhealth-group" normalise to the same key, so
#: spelling and punctuation do not matter. Matching is EXACT after
#: normalisation, never a prefix, so blocking "apple" leaves "Apple Roofing",
#: "Appletree Dental" and "Applewood Centers" alone.
BLOCKED_COMPANIES = tuple(
    s.strip() for s in _env_str("JOBSCRAPE_BLOCKED_COMPANIES", "").split(",") if s.strip()
)

# --- Logging / observability ----------------------------------------------
#: "text" (human, the default) or "json" (one object per line, for shipping).
LOG_FORMAT = _env_str("JOBSCRAPE_LOG_FORMAT", "text")
LOG_LEVEL = _env_str("JOBSCRAPE_LOG_LEVEL", "INFO")

# --- Contact / politeness -------------------------------------------------
# A descriptive UA with a contact address is the courteous default when
# hitting public endpoints; several providers ask for one explicitly.
#
# `_env_str`, not a bare `os.environ.get(name, default)`: the dashboard's
# Settings form submits every field it renders, including ones the user never
# touched, so a value that was merely blank in `.env` (rather than absent)
# must still fall back to the default here — `os.environ.get` only does that
# when the key is missing entirely, not when it's present-but-empty.
CONTACT_EMAIL = _env_str("JOBSCRAPE_CONTACT", "jobscrape@example.com")
USER_AGENT = _env_str("JOBSCRAPE_UA", f"jobscrape/1.0 (+{CONTACT_EMAIL})")

# --- HTTP tunables --------------------------------------------------------
GLOBAL_CONCURRENCY = _env_int("JOBSCRAPE_CONCURRENCY", 16)
PER_HOST_CONCURRENCY = _env_int("JOBSCRAPE_PER_HOST_CONCURRENCY", 4)
PER_HOST_DELAY = _env_float("JOBSCRAPE_PER_HOST_DELAY", 0.25)

# Hosts that need gentler treatment than the default. Every company on these
# ATSes shares one hostname, so a broad sweep concentrates all its traffic on a
# single endpoint. Ashby in particular returns 429s under the default rate, and
# a rate-limited board is a board whose jobs we never see — being slower here is
# strictly better than losing data.
HOST_LIMITS: dict[str, tuple[int, float]] = {
    # host: (max concurrent requests, minimum seconds between requests)
    "jobs.ashbyhq.com": (2, 0.7),
    "api.smartrecruiters.com": (3, 0.4),
    "apply.workable.com": (3, 0.4),
    "api.lever.co": (3, 0.3),
    "index.commoncrawl.org": (2, 1.0),
    # Small free services — deliberately gentle so we stay welcome.
    "himalayas.app": (2, 0.5),
    "remotive.com": (1, 1.0),
    "jobicy.com": (1, 1.0),
    "weworkremotely.com": (1, 1.0),
    # Certificate-transparency lookups: free tiers with tight quotas.
    "api.certspotter.com": (1, 2.0),
    "api.hackertarget.com": (1, 3.0),
    "crt.sh": (1, 3.0),
}
HTTP_TIMEOUT = _env_float("JOBSCRAPE_TIMEOUT", 45.0)
MAX_RETRIES = _env_int("JOBSCRAPE_MAX_RETRIES", 4)
CACHE_TTL_SECONDS = _env_int("JOBSCRAPE_CACHE_TTL", 6 * 3600)
CACHE_ENABLED = os.environ.get("JOBSCRAPE_CACHE", "1") not in ("0", "false", "no")

# --- Optional API keys ----------------------------------------------------
USAJOBS_API_KEY = os.environ.get("USAJOBS_API_KEY", "")
USAJOBS_EMAIL = _env_str("USAJOBS_EMAIL", CONTACT_EMAIL)
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")
JOOBLE_API_KEY = os.environ.get("JOOBLE_API_KEY", "")
# JSearch on RapidAPI: aggregated LinkedIn/Indeed/Glassdoor/ZipRecruiter.
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
# Its free tier is ~200 requests/month, so the default budget is deliberately
# small — one run should not consume the month's quota.
JSEARCH_MAX_REQUESTS = _env_int("JSEARCH_MAX_REQUESTS", 6)
# Which RapidAPI job API to call. Overridable because these come and go — the
# default host currently has no registered endpoints on RapidAPI's side.
RAPIDAPI_HOST = _env_str("RAPIDAPI_HOST", "jsearch.p.rapidapi.com")
RAPIDAPI_PATH = _env_str("RAPIDAPI_PATH", "/search")

# --- Apify ----------------------------------------------------------------
#
# Indeed and LinkedIn postings, bought from a vendor that does the scraping.
# This is the only working route to those two: the restricted scrapers in
# `sources/restricted/` are blocked by anti-bot defences, and JSearch — written
# as the compliant alternative — has produced zero rows because its provider
# returns 404 for every path.
#
# Apify's own scheduler runs the actors and calls the webhook receiver
# (`jobscrape webhook`) on success. The webhook carries no job data, only a run
# id, so the token is required to fetch the dataset afterwards.
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
#: Shared secret Apify must present, as an `X-Apify-Secret` header or the last
#: path segment. Apify offers no request signing, so this is the only
#: authentication available; their docs recommend exactly this.
APIFY_WEBHOOK_SECRET = os.environ.get("APIFY_WEBHOOK_SECRET", "")
#: Overridable so a fork, a pinned version, or a different actor entirely needs
#: no code change. Apify accepts either `user~actor-name` or the actor id.
APIFY_INDEED_ACTOR = _env_str("APIFY_INDEED_ACTOR", "cheap_scraper~indeed-job-scraper")
APIFY_LINKEDIN_ACTOR = _env_str("APIFY_LINKEDIN_ACTOR", "cheap_scraper~linkedin-job-scraper")
APIFY_API = _env_str("APIFY_API", "https://api.apify.com/v2")
#: Dataset page size. Apify enforces no maximum, but a bounded page keeps one
#: enormous run from being held in memory whole.
APIFY_PAGE_SIZE = _env_int("APIFY_PAGE_SIZE", 500)

# --- Common Crawl ---------------------------------------------------------
CC_COLLECTIONS = _env_int("JOBSCRAPE_CC_COLLECTIONS", 3)
CC_INDEX_ROOT = "https://index.commoncrawl.org"

# --- Workday --------------------------------------------------------------
# Workday has no "list everything" call, but an EMPTY search string enumerates
# the whole board — measured far better than keyword search on every tenant
# tried (AIG 475 vs 28, 3M 655 vs 41, NVIDIA 2,000 vs 1,694). Listing is
# paginated 20 at a time, so this caps how deep a single board is walked:
# 1,000 = up to 50 list requests for the largest employers, and no cost at all
# for the many boards that hold fewer postings than the cap.
#: Per-BRANCH cap, not per-board. Workday reports `total` as at most 2,000
#: however many postings a tenant really has, so the adapter narrows by facet
#: until each branch reports a real count (see sources/workday.py). This bounds
#: how deep any one branch is paged; the board total is the union of branches.
WORKDAY_MAX_PER_BOARD = _env_int("JOBSCRAPE_WORKDAY_MAX_PER_BOARD", 2000)

# --- Global-source pagination caps ----------------------------------------
# All three were verified to actually bind — the source still had more to give
# when the limit stopped us — so these are real ceilings, not theoretical ones.
# Measured on the last full sweep:
#
#   jooble      6,214 seen against a 30 x 11-term ceiling; it reports ~196,000
#               matches for a SINGLE term, and keeps 83% of what it returns —
#               the best ratio of any source here. Raised to 60 and to be
#               re-measured: the marginal page block has to keep paying.
#   himalayas   2,999 seen against a ceiling of exactly 3,000. Squarely capped,
#               but only 8% survives the filters, so this is a cheap small gain.
#   adzuna      9,799 seen against 20 x 50 x 11 = 11,000. NOT raised. The free
#               tier is quota-limited and 11 terms x 20 pages already spends
#               ~220 calls a run; the budget belongs in DEFAULT_QUERIES instead,
#               where page 1 of a term not yet searched beats page 20 of one
#               already exhausted. Configurable only so it stops being the odd
#               one out, hard-coded where its two peers were tunable.
#
# Raising any of them costs proportionally more requests, and the role/region/
# date filters still trim whatever comes back.
HIMALAYAS_MAX_PAGES = _env_int("JOBSCRAPE_HIMALAYAS_MAX_PAGES", 300)
JOOBLE_MAX_PAGES = _env_int("JOBSCRAPE_JOOBLE_MAX_PAGES", 60)
ADZUNA_MAX_PAGES = _env_int("JOBSCRAPE_ADZUNA_MAX_PAGES", 20)
#: Total Adzuna requests one fetch may spend, divided across the search terms.
#: 220 is what the old fixed 20-pages x 11-terms already cost, so widening
#: DEFAULT_QUERIES re-spreads that spend instead of multiplying it past the free
#: tier's daily quota. See sources/adzuna.py:CALL_BUDGET.
ADZUNA_CALL_BUDGET = _env_int("JOBSCRAPE_ADZUNA_CALL_BUDGET", 220)


def ensure_dirs() -> None:
    for path in (DATA_DIR, CACHE_DIR, EXPORT_DIR):
        path.mkdir(parents=True, exist_ok=True)
