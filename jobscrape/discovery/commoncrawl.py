"""Harvest ATS company slugs from the Common Crawl URL index.

This is what makes "all jobs" tractable: rather than guessing company names, we
read the set of ATS board URLs the web crawler has actually seen, and recover
each company's slug from the path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Iterable

from .. import config
from ..http import Fetcher
from .seeds import SEED_BOARDS

log = logging.getLogger("jobscrape.discovery")

# Slugs that are obviously not companies. The index is full of numeric ids,
# API paths and junk, and probing each one costs a request.
_BAD_SLUG = re.compile(
    r"^(\d+|embed|api|v\d|assets?|static|images?|css|js|favicon\.ico|robots\.txt|"
    r"sitemap.*|index|null|undefined|test|demo|www|search|jobs?|careers?|"
    r"boards?|apply|widget|error|404|500|admin|login|signup|account|.*\.(js|css|png|jpg|svg|ico|xml|txt|json))$",
    re.I,
)


def _clean(slug: str) -> str | None:
    slug = slug.strip().strip("/").lower()
    if not slug or len(slug) < 2 or len(slug) > 64:
        return None
    if _BAD_SLUG.match(slug):
        return None
    # Hash-like junk: the index contains plenty of random identifiers. Real
    # company slugs are vowel-rich, so a long unbroken string with very few
    # vowels is almost certainly not one. Both thresholds are deliberately
    # loose — a wasted probe costs one request, a wrongly rejected company
    # costs an entire board.
    if len(slug) > 12 and not re.search(r"[aeiou]", slug):
        return None
    if len(slug) > 20 and not re.search(r"[-_.]", slug):
        vowels = sum(ch in "aeiou" for ch in slug)
        if vowels / len(slug) < 0.15:
            return None
    if not re.match(r"^[a-z0-9][a-z0-9_.-]*$", slug):
        return None
    return slug


# (index query pattern, ats name, regex capturing the slug from a matched URL)
#
# Most ATSes are best queried by their exact board host. Lever is the
# exception: in recent crawls ``jobs.lever.co/*`` contains almost nothing but
# robots.txt, while the ``*.lever.co/*`` wildcard surfaces real boards.
TARGETS: list[tuple[str, str, re.Pattern[str]]] = [
    ("job-boards.greenhouse.io/*", "greenhouse",
     re.compile(r"job-boards\.greenhouse\.io/([^/?#]+)", re.I)),
    ("boards.greenhouse.io/*", "greenhouse",
     re.compile(r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?([^/?#&]+)", re.I)),
    ("*.lever.co/*", "lever",
     re.compile(r"jobs(?:\.eu)?\.lever\.co/([^/?#]+)", re.I)),
    ("jobs.ashbyhq.com/*", "ashby",
     re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)", re.I)),
    ("jobs.smartrecruiters.com/*", "smartrecruiters",
     re.compile(r"jobs\.smartrecruiters\.com/([^/?#]+)", re.I)),
    ("careers.smartrecruiters.com/*", "smartrecruiters",
     re.compile(r"careers\.smartrecruiters\.com/([^/?#]+)", re.I)),
    ("apply.workable.com/*", "workable",
     re.compile(r"apply\.workable\.com/([^/?#]+)", re.I)),
    # --- platforms verified during the coverage expansion ---
    ("*.bamboohr.com/*", "bamboohr",
     re.compile(r"https?://([a-z0-9-]+)\.bamboohr\.com", re.I)),
    ("*.breezy.hr/*", "breezy",
     re.compile(r"https?://([a-z0-9-]+)\.breezy\.hr", re.I)),
    ("*.recruitee.com/*", "recruitee",
     re.compile(r"https?://([a-z0-9-]+)\.recruitee\.com", re.I)),
    ("*.teamtailor.com/*", "teamtailor",
     re.compile(r"https?://([a-z0-9-]+)\.teamtailor\.com", re.I)),
    ("*.pinpointhq.com/*", "pinpoint",
     re.compile(r"https?://([a-z0-9-]+)\.pinpointhq\.com", re.I)),
    # Personio tenants exist on both TLDs; the adapter tries each.
    ("*.personio.de/*", "personio",
     re.compile(r"https?://([a-z0-9-]+)\.jobs\.personio\.de", re.I)),
    ("*.personio.com/*", "personio",
     re.compile(r"https?://([a-z0-9-]+)\.jobs\.personio\.com", re.I)),
    ("*.applytojob.com/*", "jazzhr",
     re.compile(r"https?://([a-z0-9-]+)\.applytojob\.com", re.I)),
    # --- enterprise ATSes added after the freehire review ---
    # iCIMS boards live at careers-<tenant>.icims.com; the capture drops the
    # `careers-` prefix so the slug matches what the adapter expects.
    ("*.icims.com/*", "icims",
     re.compile(r"https?://careers-([a-z0-9-]+)\.icims\.com", re.I)),
    ("jobs.jobvite.com/*", "jobvite",
     re.compile(r"jobs\.jobvite\.com/([^/?#]+)", re.I)),
    ("ats.rippling.com/*", "rippling",
     re.compile(r"ats\.rippling\.com/([^/?#]+)", re.I)),
    # Oracle and UKG boards are host+path triples rather than bare slugs, so
    # the crawl index cannot yield a usable board id on its own — they are
    # discovered from curated lists instead. Listing them here would harvest
    # slugs the adapter cannot address.
    # Restricted tier, but discovered the same way so slugs are ready if the
    # user opts in with --include-restricted.
    ("*.avature.net/*", "avature",
     re.compile(r"https?://([a-z0-9-]+)\.avature\.net", re.I)),
]

# Subdomains that are the vendor's own site rather than a customer board.
#
# The second block is vendor *infrastructure*, added after 20 of these were
# found sitting in the registry as iCIMS "boards" — `analytics`, `billing`,
# `autodiscover` and eleven `api-<region>` variants. Each is probed on every
# validate pass and can never be anything but dead, so they are junk that costs
# request budget forever rather than junk that merely looks untidy.
_VENDOR_SUBDOMAINS = {
    "www", "app", "api", "help", "support", "docs", "blog", "status", "developers",
    "partners", "marketing", "resources", "academy", "community", "login", "auth",
    "secure", "static", "assets", "cdn", "media", "images", "email", "go", "info",
    "about", "careers", "jobs", "hire", "hiring", "talent", "my", "portal",
    "dashboard", "admin", "test", "demo", "staging", "sandbox", "preview",
    # vendor infrastructure
    "analytics", "billing", "events", "click", "autodiscover", "gslink", "agents",
    "dev", "qa", "uat", "mail", "smtp", "ns1", "ns2", "vpn", "cpanel", "webmail",
    "monitor", "metrics", "grafana", "internal-api", "edge", "origin",
}

#: A label that is *only* vendor infrastructure once its trailing qualifier is
#: removed — `api-eu-west-1`, `api-stg`, `analytics-api`. Checked separately
#: from the set above because the suffix is unbounded: there is one per region,
#: per stage, per version, and enumerating them would be a losing game.
_VENDOR_PREFIXES = ("api-", "analytics-", "cdn-", "static-", "mail-", "smtp-",
                    "monitor-", "internal-api-")


def is_vendor_subdomain(slug: str) -> bool:
    """Whether a discovered label is the ATS's own infrastructure, not a tenant."""
    return slug in _VENDOR_SUBDOMAINS or slug.startswith(_VENDOR_PREFIXES)


# Workday tenants live on per-company subdomains, so the slug is the host.
# Career-site URLs optionally carry a locale segment before the site name
# (".../en-US/External_Careers/job/..."), which must not be mistaken for it.
WORKDAY_HOST_RE = re.compile(
    r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Za-z]{2}/)?"
    r"([^/?#]+)",
    re.I,
)


#: Where a long sweep records the (collection, pattern, page) units it finished
#: AND the slugs harvested so far, so an interrupted run resumes instead of
#: repeating hours of work.
#:
#: Both together, not just the done-set: `discover_all`'s caller only writes
#: harvested slugs to the company registry once, after the WHOLE multi-hour,
#: multi-collection sweep returns (see `cli.cmd_discover`). A page's slugs
#: used to live only in an in-memory `set` that `harvest_ats`/`harvest_workday`
#: returned at the very end — so a crash partway through left the checkpoint
#: recording pages as "done" whose slugs were never persisted anywhere. On
#: resume, those pages were skipped (correctly, per the checkpoint) but their
#: slugs were never re-extracted either, since nothing re-visits a page once
#: skipped — permanently and silently losing that page's companies. Rolling
#: the accumulated slugs into the SAME checkpoint file, updated at the same
#: point `done` is, means a resumed run recovers exactly what a finished page
#: already found, not just the fact that it was visited.
PROGRESS_PATH = config.DATA_DIR / "cc_progress.json"


def _load_progress() -> tuple[set[str], dict[str, set[str]]]:
    try:
        data = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        done = set(data.get("done") or [])
        results = {ats: set(slugs) for ats, slugs in (data.get("results") or {}).items()}
        return done, results
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        return set(), {}


def _save_progress(done: set[str], results: dict[str, set[str]]) -> None:
    try:
        PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "done": sorted(done),
            "results": {ats: sorted(slugs) for ats, slugs in results.items()},
        }
        PROGRESS_PATH.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


async def list_collections(fetcher: Fetcher, limit: int) -> list[str]:
    """Return the ids of the most recent Common Crawl collections.

    Retries with a long backoff: the index periodically refuses connections
    outright when it is rate-limiting, and a multi-hour sweep should ride that
    out rather than abandon the run.
    """
    for attempt in range(3):
        data = await fetcher.get_json(f"{config.CC_INDEX_ROOT}/collinfo.json", retries=2)
        if isinstance(data, list):
            return [c["id"] for c in data[:limit] if "id" in c]
        if attempt < 2:
            delay = 30 * (attempt + 1)
            log.warning(
                "Common Crawl index not responding; retrying in %ds (attempt %d/3)",
                delay, attempt + 2,
            )
            await asyncio.sleep(delay)
    log.warning("Could not read Common Crawl collection list")
    return []


async def _index_pages(fetcher: Fetcher, collection: str, url_pattern: str) -> int:
    """How many pages the index holds for this pattern (0 if unavailable)."""
    data = await fetcher.get_json(
        f"{config.CC_INDEX_ROOT}/{collection}-index",
        params={"url": url_pattern, "output": "json", "showNumPages": "true"},
    )
    if isinstance(data, dict):
        return int(data.get("pages", 0) or 0)
    return 0


def _extract(lines: Iterable[str], pattern: re.Pattern[str]) -> set[str]:
    found: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        url = record.get("url", "")
        match = pattern.search(url)
        if match:
            slug = _clean(match.group(1))
            # For subdomain-per-customer platforms the vendor's own subdomains
            # match the pattern too; probing "www.bamboohr.com" as a company
            # wastes a request every run.
            if slug and not is_vendor_subdomain(slug):
                found.add(slug)
    return found


async def harvest_ats(
    fetcher: Fetcher,
    collection: str,
    url_pattern: str,
    ats: str,
    pattern: re.Pattern[str],
    max_pages: int | None = None,
    done: set[str] | None = None,
    results: dict[str, set[str]] | None = None,
) -> set[str]:
    """Pull every slug for one ATS host pattern from one crawl collection.

    ``done`` is the resume ledger: page units already recorded there are
    skipped. ``results`` (keyed by ``ats``, shared across the whole sweep) is
    where a page's slugs are written AS SOON as they're extracted — same
    point ``done`` gets the page added and both get flushed to disk together,
    so a crash never leaves the checkpoint claiming a page is done while its
    slugs exist only in memory. Pre-seeded with a resumed run's own prior
    slugs, since a skipped page contributes nothing to the LOCAL accumulator
    below otherwise.
    """
    pages = await _index_pages(fetcher, collection, url_pattern)
    if not pages:
        return set()
    if max_pages:
        pages = min(pages, max_pages)

    # Writing straight into `results[ats]` (rather than a fresh local set
    # merged in by the caller afterward) is what lets a resumed run recover a
    # skipped page's slugs and lets `_save_progress` checkpoint them alongside
    # `done` at the same point, every page.
    slugs = results.setdefault(ats, set()) if results is not None else set()
    skipped = 0
    for page in range(pages):
        unit = f"{collection}|{url_pattern}|{page}"
        if done is not None and unit in done:
            skipped += 1
            continue
        text = await fetcher.get_text(
            f"{config.CC_INDEX_ROOT}/{collection}-index",
            params={"url": url_pattern, "output": "json", "page": str(page)},
        )
        if not text:
            # Leave it unmarked so a later run retries this page.
            continue
        slugs |= _extract(text.splitlines(), pattern)
        if done is not None:
            done.add(unit)
            _save_progress(done, results or {})
    note = f"{pages}p" + (f" ({skipped} cached)" if skipped else "")
    log.info("  %-28s %-16s %s: %d slugs", url_pattern, collection, note, len(slugs))
    return slugs


async def harvest_workday(
    fetcher: Fetcher,
    collection: str,
    max_pages: int | None = None,
    done: set[str] | None = None,
    results: dict[str, set[str]] | None = None,
) -> set[str]:
    """Workday tenants: capture ``tenant|wd-number|site`` triples.

    ``results["workday"]`` (shared across the whole sweep, same as
    :func:`harvest_ats`) is where each page's tenants are written directly,
    checkpointed alongside ``done`` immediately after — extraction now runs
    BEFORE the page is marked done, not after: it used to check the page off
    right after the fetch but before parsing it at all, so a crash during
    parsing left a page checkpointed "done" with none of its tenants ever
    captured.
    """
    url_pattern = "*.myworkdayjobs.com/*"
    pages = await _index_pages(fetcher, collection, url_pattern)
    if not pages:
        return set()
    if max_pages:
        pages = min(pages, max_pages)

    found = results.setdefault("workday", set()) if results is not None else set()
    for page in range(pages):
        unit = f"{collection}|{url_pattern}|{page}"
        if done is not None and unit in done:
            continue
        text = await fetcher.get_text(
            f"{config.CC_INDEX_ROOT}/{collection}-index",
            params={"url": url_pattern, "output": "json", "page": str(page)},
        )
        if not text:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                url = json.loads(line).get("url", "")
            except json.JSONDecodeError:
                continue
            match = WORKDAY_HOST_RE.search(url)
            if not match:
                continue
            tenant, wd, site = match.group(1).lower(), match.group(2).lower(), match.group(3)
            # Skip non-site paths that share the URL shape.
            if site.lower() in {"robots.txt", "sitemap.xml", "favicon.ico", "wday"}:
                continue
            if _clean(tenant) and site and len(site) < 80:
                found.add(f"{tenant}|{wd}|{site}")
        if done is not None:
            done.add(unit)
            _save_progress(done, results or {})

    deduped = _dedupe_workday_case_variants(found)
    if results is not None:
        results["workday"] = deduped
    log.info("  %-28s %-16s %s: %d tenants", "myworkdayjobs.com", collection, f"{pages}p", len(deduped))
    return deduped


def _dedupe_workday_case_variants(slugs: set[str]) -> set[str]:
    """Collapse site names that differ only by letter case.

    The index yields both ``3m|wd1|Search`` and ``3m|wd1|search`` because URLs
    get case-normalized somewhere upstream; probing both doubles the work for
    one board. Genuinely different sites for the same tenant (``AES_US`` vs
    ``AES_ANDES``) are distinct career sites and are all kept.

    When two spellings collide we keep the mixed-case one — Workday site names
    are conventionally CamelCase, so an all-lowercase variant is usually the
    normalized artifact rather than the real name.
    """
    best: dict[tuple[str, str, str], str] = {}
    for slug in slugs:
        parts = slug.split("|")
        if len(parts) != 3:
            continue
        tenant, wd, site = parts
        key = (tenant, wd, site.lower())
        current = best.get(key)
        if current is None:
            best[key] = slug
            continue
        current_site = current.split("|")[2]
        if current_site.islower() and not site.islower():
            best[key] = slug
    return set(best.values())


async def discover_all(
    fetcher: Fetcher,
    collections: int | None = None,
    max_pages: int | None = None,
    include_workday: bool = True,
    include_seeds: bool = True,
    resume: bool = True,
) -> dict[str, set[str]]:
    """Harvest slugs for every supported ATS across recent crawl collections.

    A full sweep is measured in hours, so completed pages are checkpointed to
    ``data/cc_progress.json`` and skipped on a later run. Pass ``resume=False``
    to force a clean re-read.

    The slugs a resumed page already found are checkpointed alongside
    ``done`` (see ``_save_progress``) and seeded into ``results`` here — a
    page recorded as done is never re-visited, so without this its slugs
    would otherwise only have existed in the crashed run's memory.
    """
    results: dict[str, set[str]] = {}
    if resume:
        done, saved = _load_progress()
        for ats, slugs in saved.items():
            results.setdefault(ats, set()).update(slugs)
    else:
        done = set()
    if done:
        log.info("Resuming: %d index pages already harvested", len(done))

    # Merge the bundled, pre-verified boards first so a weak index response
    # still leaves the caller with a usable baseline.
    if include_seeds:
        for ats, slugs in SEED_BOARDS.items():
            results.setdefault(ats, set()).update(slugs)
        log.info("Seeded %d known boards", sum(len(v) for v in SEED_BOARDS.values()))

    ids = await list_collections(fetcher, collections or config.CC_COLLECTIONS)
    if not ids:
        log.warning("Common Crawl index unreachable — continuing with seeds only")
        return results
    log.info("Harvesting from %d Common Crawl collection(s): %s", len(ids), ", ".join(ids))

    for collection in ids:
        for url_pattern, ats, pattern in TARGETS:
            # Both functions write into `results` directly now (see their
            # own docstrings) — no merge needed here, unlike before.
            await harvest_ats(
                fetcher, collection, url_pattern, ats, pattern, max_pages, done, results
            )
        if include_workday:
            await harvest_workday(fetcher, collection, max_pages, done, results)
    return results
