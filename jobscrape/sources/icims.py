"""iCIMS career sites.

iCIMS exposes no public JSON API, but every board publishes a sitemap listing
each posting's URL, and each posting page carries a schema.org ``JobPosting``
block. So: one sitemap fetch, then one detail fetch per posting.

A board is either an iCIMS slug (``360care`` → ``careers-360care.icims.com``)
or a vanity host on the newer "careers-home" product (``careers.docusign.com``),
told apart by whether it contains a dot.
"""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree as ET

from ..http import Fetcher, gather_limited
from ..models import Job
from . import jsonld
from .base import BoardSource, Prefilter, shallow_refresh

#: A posting URL, across the three shapes iCIMS serves:
#:   /jobs/5059/helpdesk-tech/job      classic subdomain board
#:   /careers-home/jobs/12345          "careers-home" product
#:   /jobs/30010?lang=en-us            vanity host, bare id
#: The numeric id is what distinguishes a posting from the board's own
#: /jobs/search page, which every sitemap also lists.
_JOB_URL_PATTERNS = (
    re.compile(r"/jobs/(\d+)/[^/]+/job", re.I),
    re.compile(r"/careers-home/jobs/(\d+)", re.I),
    re.compile(r"/jobs/(\d+)(?:[?#]|$)", re.I),
)


def _job_id(url: str) -> str | None:
    for pattern in _JOB_URL_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None

#: Runaway guard — the largest iCIMS tenants run to a few thousand postings.
MAX_PER_BOARD = 1500


def board_host(slug: str) -> str:
    """Resolve a board id to its host.

    A dotted board is already a vanity host. A bare slug becomes
    ``careers-<slug>.icims.com`` — but idempotently, because certificate
    transparency yields the *whole* subdomain label (``careers-360care``)
    while Common Crawl's regex yields just the tenant (``360care``). Without
    the guard, one vector's slugs would resolve to
    ``careers-careers-360care.icims.com`` and every board it found would look
    dead.
    """
    if "." in slug:
        return slug
    tenant = slug[len("careers-"):] if slug.startswith("careers-") else slug
    return f"careers-{tenant}.icims.com"


def _sitemap_urls(xml: str | None) -> list[str]:
    """Pull `<loc>` values out of a sitemap, tolerating a malformed document."""
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    # The namespace is declared on the root, so match on the local name rather
    # than hardcoding the sitemaps.org URI, which some tenants omit or vary.
    return [
        (el.text or "").strip()
        for el in root.iter()
        if el.tag.rsplit("}", 1)[-1] == "loc" and (el.text or "").strip()
    ]


class ICIMS(BoardSource):
    name = "icims"
    ats = "icims"
    note = "Sitemap-driven; each posting page carries JSON-LD."

    async def _detail(
        self, fetcher: Fetcher, url: str
    ) -> tuple[str, dict[str, Any] | None]:
        # `in_iframe=1` is what makes iCIMS serve the bare posting rather than
        # its chrome, and it is where the JSON-LD block lives.
        separator = "&" if "?" in url else "?"
        html = await fetcher.get_text(f"{url}{separator}in_iframe=1")
        return url, jsonld.first_job_posting(html)

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        known_ids: set[str] | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        host = board_host(slug)
        xml = await fetcher.get_text(f"https://{host}/sitemap.xml")
        if xml is None:
            return None

        urls = _sitemap_urls(xml)
        # Vanity hosts on the "careers-home" product serve a sitemap INDEX whose
        # entries are further sitemaps, not postings — reading it as a flat
        # sitemap finds nothing and the board silently reads as empty. Follow
        # one level; nobody nests deeper, and an unbounded walk on a
        # third-party document is not something to invite.
        if not any(_job_id(u) for u in urls):
            nested = await gather_limited(
                [fetcher.get_text(u) for u in urls[:20]], limit=4
            )
            urls = [u for doc in nested if isinstance(doc, str) for u in _sitemap_urls(doc)]

        found: list[tuple[str, str]] = []  # (job_id, url)
        for url in urls:
            job_id = _job_id(url)
            if job_id:
                found.append((job_id, url))
        # A sitemap can list the same posting under several paths.
        seen_ids: set[str] = set()
        postings = [
            (jid, url) for jid, url in found
            if not (jid in seen_ids or seen_ids.add(jid))
        ][:MAX_PER_BOARD]

        if not postings:
            return []

        known = known_ids or set()
        details: dict[str, dict[str, Any]] = {}
        if with_details:
            needed = [(jid, url) for jid, url in postings if jid not in known]
            fetched = await gather_limited(
                [self._detail(fetcher, url) for _, url in needed], limit=4
            )
            by_url = {
                r[0]: r[1] for r in fetched
                if isinstance(r, tuple) and isinstance(r[1], dict)
            }
            for jid, url in needed:
                if url in by_url:
                    details[jid] = by_url[url]

        jobs: list[Job] = []
        for job_id, url in postings:
            posting = details.get(job_id)
            lo, hi, currency = jsonld.salary(posting)
            job = Job(
                source=self.name,
                source_job_id=f"{slug}:{job_id}",
                title=(posting or {}).get("title") or "",
                company=jsonld.hiring_org(posting) or slug,
                company_slug=slug,
                job_url=url,
                location_raw=jsonld.location_text(posting),
                is_remote=jsonld.is_remote(posting),
                employment_type=jsonld.employment_type(posting),
                department=(posting or {}).get("occupationalCategory"),
                posted_at=(posting or {}).get("datePosted"),
                salary_min=lo,
                salary_max=hi,
                salary_currency=currency,
                description_html=jsonld.description(posting),
                raw=posting or {},
            )
            if job_id in known:
                job = shallow_refresh(job)
            jobs.append(job)
        return jobs
