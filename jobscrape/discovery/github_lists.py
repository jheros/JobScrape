"""Harvest ATS company slugs from curated GitHub job lists.

Community-maintained new-grad and internship repos link directly to thousands of
Greenhouse/Lever/Ashby postings. The yield is small next to Common Crawl, but the
quality is high — every entry is a real, currently-hiring tech employer — and it
costs a handful of requests.

It is also independent of the Common Crawl index, so it keeps working when that
index is unavailable.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from ..http import Fetcher
from .commoncrawl import _clean, is_vendor_subdomain

log = logging.getLogger("jobscrape.github_lists")

# Raw README URLs. Both `main` and `dev` are tried since repos differ.
REPOS = (
    ("SimplifyJobs/New-Grad-Positions", ("dev", "main")),
    ("SimplifyJobs/Summer2026-Internships", ("dev", "main")),
    ("speedyapply/2026-SWE-College-Jobs", ("main",)),
    ("vanshb03/Summer2026-Internships", ("dev", "main")),
    ("Ouckah/Summer2026-Internships", ("main",)),
)
RAW = "https://raw.githubusercontent.com/{repo}/{branch}/README.md"

# Path-style boards: the slug is the first path segment.
_PATH_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([A-Za-z0-9_.-]+)", re.I), "greenhouse"),
    (re.compile(r"jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_.-]+)", re.I), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)", re.I), "ashby"),
    (re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_.-]+)", re.I), "smartrecruiters"),
    (re.compile(r"apply\.workable\.com/([A-Za-z0-9_.-]+)", re.I), "workable"),
)

# Subdomain-style boards: the slug is the subdomain label.
_HOST_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"https?://([a-z0-9-]+)\.bamboohr\.com", re.I), "bamboohr"),
    (re.compile(r"https?://([a-z0-9-]+)\.breezy\.hr", re.I), "breezy"),
    (re.compile(r"https?://([a-z0-9-]+)\.recruitee\.com", re.I), "recruitee"),
    (re.compile(r"https?://([a-z0-9-]+)\.teamtailor\.com", re.I), "teamtailor"),
    (re.compile(r"https?://([a-z0-9-]+)\.pinpointhq\.com", re.I), "pinpoint"),
    (re.compile(r"https?://([a-z0-9-]+)\.applytojob\.com", re.I), "jazzhr"),
)

# Workday needs the tenant|wd|site triple rather than a bare slug.
_WORKDAY_RE = re.compile(
    r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Za-z]{2}/)?([^/?#\s\)\]]+)",
    re.I,
)


def extract(text: str) -> dict[str, set[str]]:
    """Pull every recognizable ATS slug out of a markdown document."""
    found: dict[str, set[str]] = {}

    def add(ats: str, slug: str | None) -> None:
        if slug:
            found.setdefault(ats, set()).add(slug)

    for pattern, ats in _PATH_PATTERNS:
        for raw in pattern.findall(text):
            add(ats, _clean(raw))

    for pattern, ats in _HOST_PATTERNS:
        for raw in pattern.findall(text):
            slug = _clean(raw)
            if slug and not is_vendor_subdomain(slug):
                add(ats, slug)

    for tenant, wd, site in _WORKDAY_RE.findall(text):
        site = site.strip().rstrip(").,")
        if _clean(tenant) and site and site.lower() not in {"robots.txt", "wday"}:
            add("workday", f"{tenant.lower()}|{wd.lower()}|{site}")

    return found


async def discover_all(
    fetcher: Fetcher, repos: Iterable[tuple[str, tuple[str, ...]]] | None = None
) -> dict[str, set[str]]:
    """Read each curated list and union the slugs found."""
    results: dict[str, set[str]] = {}
    for repo, branches in repos or REPOS:
        text = None
        for branch in branches:
            text = await fetcher.get_text(RAW.format(repo=repo, branch=branch), retries=1)
            if text:
                break
        if not text:
            log.info("  %-42s unavailable", repo)
            continue

        found = extract(text)
        total = sum(len(v) for v in found.values())
        for ats, slugs in found.items():
            results.setdefault(ats, set()).update(slugs)
        log.info("  %-42s %4d slugs %s", repo, total,
                 {k: len(v) for k, v in sorted(found.items())})
    return results
