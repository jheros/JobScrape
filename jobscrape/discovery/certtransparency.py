"""Discover ATS customers through TLS certificate transparency.

Every platform that gives each customer its own subdomain
(``acme.teamtailor.com``) must obtain a certificate for it, and every issued
certificate is published to public CT logs. Querying those logs therefore
enumerates customers that the Common Crawl index never happened to crawl.

Measured while building this: 79 unique Teamtailor subdomains versus the 6 that
Common Crawl had produced — a 13x improvement.

Three providers, tried in order of usefulness. All have tight free-tier quotas,
so :data:`jobscrape.config.HOST_LIMITS` throttles them hard and a failure of one
never blocks the others.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable

from ..http import Fetcher
from .commoncrawl import _VENDOR_SUBDOMAINS, _clean

log = logging.getLogger("jobscrape.certtransparency")

CERTSPOTTER = "https://api.certspotter.com/v1/issuances"
HACKERTARGET = "https://api.hackertarget.com/hostsearch/"
CRTSH = "https://crt.sh/"

#: ATS domain -> the `ats` key used in the companies table.
CT_TARGETS: dict[str, str] = {
    "bamboohr.com": "bamboohr",
    "breezy.hr": "breezy",
    "teamtailor.com": "teamtailor",
    "recruitee.com": "recruitee",
    "pinpointhq.com": "pinpoint",
    "applytojob.com": "jazzhr",
    "jobs.personio.de": "personio",
    "jobs.personio.com": "personio",
    "avature.net": "avature",
    # Every iCIMS tenant gets its own certificate, so CT is the densest source
    # of them. The label comes back whole (`careers-360care`); the adapter's
    # board_host() strips the prefix rather than double-applying it.
    "icims.com": "icims",
}


def _subdomains(names: Iterable[str], domain: str) -> set[str]:
    """Reduce hostnames to the single label that identifies a customer.

    Multi-label names (``careers.eu.acme.teamtailor.com``) are skipped rather
    than guessed at — the board URL only ever uses the one-label form.
    """
    suffix = f".{domain}"
    found: set[str] = set()
    for name in names:
        host = (name or "").strip().lower().lstrip("*.")
        if not host.endswith(suffix):
            continue
        label = host[: -len(suffix)]
        if not label or "." in label:
            continue
        slug = _clean(label)
        if slug and slug not in _VENDOR_SUBDOMAINS:
            found.add(slug)
    return found


async def from_certspotter(fetcher: Fetcher, domain: str) -> set[str]:
    data = await fetcher.get_json(
        CERTSPOTTER,
        params={
            "domain": domain,
            "include_subdomains": "true",
            "expand": "dns_names",
        },
        retries=1,
    )
    if not isinstance(data, list):
        return set()
    names: list[str] = []
    for entry in data:
        if isinstance(entry, dict):
            names.extend(entry.get("dns_names") or [])
    return _subdomains(names, domain)


async def from_hackertarget(fetcher: Fetcher, domain: str) -> set[str]:
    text = await fetcher.get_text(HACKERTARGET, params={"q": domain}, retries=1)
    if not text or "error" in text[:60].lower() or "API count exceeded" in text:
        return set()
    # Plain CSV: "host,ip" per line.
    return _subdomains((line.split(",")[0] for line in text.splitlines()), domain)


async def from_crtsh(fetcher: Fetcher, domain: str) -> set[str]:
    """crt.sh is the richest source but frequently returns 404/502 under load."""
    text = await fetcher.get_text(
        CRTSH, params={"q": f"%.{domain}", "output": "json"}, retries=1
    )
    if not text or not text.lstrip().startswith("["):
        return set()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return set()
    names: list[str] = []
    for entry in data:
        if isinstance(entry, dict):
            names.extend((entry.get("name_value") or "").split("\n"))
    return _subdomains(names, domain)


async def discover_domain(fetcher: Fetcher, domain: str) -> set[str]:
    """Union every provider that responds for one ATS domain."""
    found: set[str] = set()
    for name, provider in (
        ("certspotter", from_certspotter),
        ("hackertarget", from_hackertarget),
        ("crt.sh", from_crtsh),
    ):
        try:
            slugs = await provider(fetcher, domain)
        except Exception as exc:  # noqa: BLE001 - one provider must not stop the rest
            log.debug("%s failed for %s: %s", name, domain, exc)
            continue
        new = slugs - found
        if slugs:
            log.info("  %-14s %-22s %4d found (%d new)", name, domain, len(slugs), len(new))
        found |= slugs
    return found


async def discover_all(
    fetcher: Fetcher, targets: dict[str, str] | None = None
) -> dict[str, set[str]]:
    """Enumerate customers for every subdomain-per-customer ATS."""
    results: dict[str, set[str]] = {}
    for domain, ats in (targets or CT_TARGETS).items():
        slugs = await discover_domain(fetcher, domain)
        if slugs:
            results.setdefault(ats, set()).update(slugs)
        log.info("%-16s %-22s -> %d slugs", ats, domain, len(slugs))
    return results
