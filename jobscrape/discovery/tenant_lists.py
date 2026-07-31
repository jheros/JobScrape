"""Import published ATS tenant lists as unchecked companies.

Discovery normally works outward from Common Crawl and certificate transparency.
A published tenant list is the same thing already done, and is by far the
cheapest coverage available: no crawling, no guessing, just names to probe.

The lists are **not trusted** — they are imported with status ``unchecked`` and
the existing ``validate`` stage decides what is real. That stage already exists
and is exactly the right gate, so nothing here needs to know whether a board
still answers.

Slug normalisation is the whole job
===================================

Every list publishes whatever identifier its own scraper happened to use, and
those are not our slugs. Workday ships ``23andme/23`` where we need
``23andme|wd5|23`` — and the ``wd5`` exists only in the URL column, so a naive
slug-column import produces boards that all 404. Six platforms need real
parsing; the other fifteen are bare slugs that pass through.

A row whose slug cannot be normalised is **skipped and counted**, never guessed
at. A wrong slug costs a probe request and lands in the archive as a dead
company; silence about how many were dropped is worse, because it makes a
partial import look complete.

Licensing
=========

Only sources whose data is licensed for reuse are read. ``ats-scrapers`` is MIT
throughout, including its ``ats-companies/`` CSVs, so it is usable with
attribution. ``job-board-aggregator`` is deliberately **not** imported: its code
is MIT but its ``data/`` directory is CC BY-NC 4.0, and this is not a
non-commercial project by declaration.

Credit: tenant lists from ats-scrapers (MIT) —
https://github.com/kalil0321/ats-scrapers
"""

from __future__ import annotations

import csv
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Callable, Iterator

log = logging.getLogger("jobscrape.tenant_lists")

#: ``platform file stem`` -> our ATS name. Only platforms we can actually fetch
#: are listed; a file with no adapter is skipped rather than imported, since an
#: unfetchable company is just a row that fails validation forever.
PLATFORM_ATS = {
    "adp": "adp",
    "ashby": "ashby",
    "bamboohr": "bamboohr",
    "breezy": "breezy",
    "cornerstone": "cornerstone",
    "greenhouse": "greenhouse",
    "icims": "icims",
    "jazzhr": "jazzhr",
    "jobvite": "jobvite",
    "lever": "lever",
    "oracle": "oracle",
    "paycom": "paycom",
    "personio": "personio",
    "pinpoint": "pinpoint",
    "recruitee": "recruitee",
    "rippling": "rippling",
    "smartrecruiters": "smartrecruiters",
    "teamtailor": "teamtailor",
    "ukg": "ukg",
    "workable": "workable",
    "workday": "workday",
}


# --- per-platform slug normalisers ----------------------------------------
#
# Each takes the CSV's (slug, url) and returns our slug, or None to skip.


def _plain(slug: str, url: str) -> str | None:
    """Bare board name. Reject anything carrying a path or scheme."""
    value = slug.strip().strip("/")
    if not value or "/" in value or ":" in value or " " in value:
        return None
    return value


_WORKDAY_URL = re.compile(
    r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[\w-]+/)?([\w-]+)/?", re.I
)


def _workday(slug: str, url: str) -> str | None:
    """``tenant|wdN|site``.

    The data-centre number (``wd5``) appears **only** in the URL — the slug
    column reads ``23andme/23``. Defaulting it to ``wd1`` would 404 for every
    tenant not hosted there, so a row whose URL does not parse is skipped.
    """
    match = _WORKDAY_URL.search(url or "")
    if not match:
        return None
    tenant, datacentre, site = match.groups()
    return f"{tenant}|{datacentre}|{site}"


_CSOD_URL = re.compile(r"https?://([\w-]+)\.csod\.com/ux/ats/careersite/(\d+)/", re.I)


def _cornerstone(slug: str, url: str) -> str | None:
    """``tenant|site_id``. The site id is part of the identity, not a default."""
    match = _CSOD_URL.search(url or "") or _CSOD_URL.search(slug or "")
    if match:
        return f"{match.group(1)}|{match.group(2)}"
    bare = _plain(slug, url)
    # A bare tenant with no site id means career site 1, Cornerstone's default.
    return f"{bare}|1" if bare else None


#: The host is NOT pinned to oraclecloud.com: tenants front the same product on
#: their own domain (careers.alithya.com, talents.henner.com), and requiring the
#: Oracle domain silently dropped those.
_ORACLE_URL = re.compile(
    r"https?://([\w.-]+)/hcmUI/CandidateExperience/[\w-]+/sites/([\w-]+)",
    re.I,
)


def _oracle(slug: str, url: str) -> str | None:
    """``host/site``. Both halves live in the URL; the slug column is a stub."""
    match = _ORACLE_URL.search(url or "")
    return f"{match.group(1)}/{match.group(2)}" if match else None


_UKG_URL = re.compile(
    r"https?://([\w.-]+)/([\w-]+)/JobBoard/([0-9a-f-]{36})", re.I
)


def _ukg(slug: str, url: str) -> str | None:
    """``host/tenant/guid``."""
    match = _UKG_URL.search(url or "") or _UKG_URL.search(slug or "")
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}/{match.group(3)}"


def _adp(slug: str, url: str) -> str | None:
    """``cid`` or ``cid:ccId`` — the list writes the pair as ``cid/ccId``."""
    match = re.search(r"[?&]cid=([0-9a-f-]{36})(?:.*?[?&]ccId=([\w]+))?", url or "", re.I)
    if match:
        return f"{match.group(1)}:{match.group(2)}" if match.group(2) else match.group(1)
    parts = (slug or "").strip().split("/")
    if parts and re.fullmatch(r"[0-9a-f-]{36}", parts[0], re.I):
        return f"{parts[0]}:{parts[1]}" if len(parts) > 1 and parts[1] else parts[0]
    return None


def _icims(slug: str, url: str) -> str | None:
    """A bare tenant, or a whole hostname when the board is not ``careers-*``.

    ``board_host`` expands a bare slug to ``careers-<slug>.icims.com`` and
    passes anything containing a dot through untouched. iCIMS boards use many
    subdomain prefixes though — ``external-``, ``resume-``, ``jobs-``,
    ``drivercareers-`` — so only the ``careers-`` form can be reduced to a
    tenant. Everything else has to keep its full hostname, or it would be
    re-expanded into a host that does not exist. Reducing them was costing 715
    of 2,498 iCIMS boards.
    """
    host = re.search(r"https?://([\w.-]+)", url or "") or re.search(r"^([\w.-]+\.[\w.-]+)$", (slug or "").strip())
    if host:
        hostname = host.group(1).lower().rstrip("/")
        plain = re.fullmatch(r"careers-([\w-]+)\.icims\.com", hostname)
        return plain.group(1) if plain else hostname
    return _plain(slug, url)


_PAYCOM_TOKEN = re.compile(r"^[0-9a-f]{32}$", re.I)


def _paycom(slug: str, url: str) -> str | None:
    value = (slug or "").strip().lower()
    if _PAYCOM_TOKEN.fullmatch(value):
        return value
    match = re.search(r"/portal/([0-9a-f]{32})", url or "", re.I)
    return match.group(1).lower() if match else None


def _jobvite(slug: str, url: str) -> str | None:
    """A bare board name. Some rows carry a ``careers/`` path prefix, which is
    part of the URL layout rather than the board id."""
    value = (slug or "").strip().strip("/")
    if value.startswith("careers/"):
        value = value[len("careers/"):]
    return _plain(value, url)


def _ashby(slug: str, url: str) -> str | None:
    """A board name, taken URL-encoded from the URL when it contains spaces.

    50 Ashby boards are named with spaces ("A1 Garage Door Service"). The slug
    column lower-cases and un-encodes them, which does not round-trip — the API
    path needs the original casing and percent-encoding.
    """
    match = re.search(r"https?://jobs\.ashbyhq\.com/([^/?#]+)", url or "")
    if match:
        return match.group(1)
    return _plain(slug, url)


NORMALISERS: dict[str, Callable[[str, str], str | None]] = {
    "jobvite": _jobvite,
    "ashby": _ashby,
    "workday": _workday,
    "cornerstone": _cornerstone,
    "oracle": _oracle,
    "ukg": _ukg,
    "adp": _adp,
    "icims": _icims,
    "paycom": _paycom,
}


def normalise(platform: str, slug: str, url: str) -> str | None:
    return NORMALISERS.get(platform, _plain)(slug or "", url or "")


# --- reading ---------------------------------------------------------------


def read_list(path: Path, platform: str) -> tuple[list[str], int]:
    """Return ``(normalised slugs, skipped count)`` for one CSV."""
    slugs: list[str] = []
    seen: set[str] = set()
    skipped = 0
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            value = normalise(platform, row.get("slug", ""), row.get("url", ""))
            if not value:
                skipped += 1
                continue
            if value not in seen:
                seen.add(value)
                slugs.append(value)
    return slugs, skipped


def iter_lists(directory: Path) -> Iterator[tuple[str, str, list[str], int]]:
    """Yield ``(platform, ats, slugs, skipped)`` for every usable CSV."""
    for path in sorted(directory.glob("*.csv")):
        platform = path.stem
        ats = PLATFORM_ATS.get(platform)
        if not ats:
            log.debug("skipping %s — no adapter", platform)
            continue
        slugs, skipped = read_list(path, platform)
        yield platform, ats, slugs, skipped


def import_directory(conn, directory: Path) -> dict[str, int]:
    """Import every usable CSV as ``unchecked`` companies.

    Returns a per-platform count of rows *newly added*. Slugs already known are
    left exactly as they are — a board that has already been validated must not
    be reset to unchecked by a re-import.
    """
    from .. import db

    added: dict[str, int] = {}
    skipped_total: Counter[str] = Counter()
    for platform, ats, slugs, skipped in iter_lists(directory):
        if not slugs:
            continue
        new = db.add_companies(conn, ats, slugs)
        added[platform] = new
        skipped_total[platform] = skipped
        log.info(
            "%-16s %6d in list, %6d new, %6d unparseable",
            platform, len(slugs), new, skipped,
        )
    total_skipped = sum(skipped_total.values())
    if total_skipped:
        log.info(
            "%d row(s) skipped as unparseable: %s",
            total_skipped,
            ", ".join(f"{p}={n}" for p, n in skipped_total.most_common() if n),
        )
    return added
