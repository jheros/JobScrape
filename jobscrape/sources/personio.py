"""Personio public job feed (XML).

The only XML source here. One request returns every posting with its full
description, so it is cheap despite the format. Tenants live on either
``.jobs.personio.de`` or ``.jobs.personio.com``; both are tried.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any

from ..http import Fetcher
from ..models import Job
from .base import BoardSource, Prefilter

log = logging.getLogger("jobscrape.personio")

HOSTS = ("de", "com")
FEED = "https://{slug}.jobs.personio.{tld}/xml"


def _text(node: ET.Element, tag: str) -> str | None:
    child = node.find(tag)
    if child is None:
        return None
    value = "".join(child.itertext()).strip()
    return value or None


def _descriptions(node: ET.Element) -> str:
    """Concatenate the labelled ``jobDescription`` blocks into one HTML body."""
    container = node.find("jobDescriptions")
    if container is None:
        return ""
    parts: list[str] = []
    for block in container.findall("jobDescription"):
        name = _text(block, "name")
        value = _text(block, "value")
        if name:
            parts.append(f"<h3>{name}</h3>")
        if value:
            parts.append(value)
    return "\n".join(parts)


class Personio(BoardSource):
    name = "personio"
    ats = "personio"
    note = "Public XML feed; every posting with full description in one request."

    async def _feed(self, fetcher: Fetcher, slug: str) -> tuple[str, str] | None:
        """Return ``(xml, tld)`` — the TLD matters for building job URLs.

        A tenant lives on exactly one of the two domains, so hardcoding ``.com``
        would emit dead links for every ``.de``-only company.
        """
        for tld in HOSTS:
            text = await fetcher.get_text(FEED.format(slug=slug, tld=tld))
            if text and text.lstrip().startswith("<"):
                return text, tld
        return None

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        feed = await self._feed(fetcher, slug)
        if not feed:
            return None
        text, tld = feed
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            log.debug("personio/%s: malformed XML: %s", slug, exc)
            return None

        jobs: list[Job] = []
        for node in root.findall("position"):
            job_id = _text(node, "id")
            title = _text(node, "name")
            if not job_id or not title:
                continue

            # additionalOffices is often whitespace-only padding rather than
            # absent, so normalize before joining.
            offices: list[str] = []
            for office in (_text(node, "office"), _text(node, "additionalOffices")):
                cleaned = " ".join((office or "").split())
                if cleaned and cleaned not in offices:
                    offices.append(cleaned)
            location_raw = "; ".join(offices) or None
            department = _text(node, "department")

            if prefilter is not None and not prefilter(title, department, location_raw):
                continue

            schedule = _text(node, "schedule")
            employment = _text(node, "employmentType")
            employment_type = ", ".join(p for p in (employment, schedule) if p) or None

            jobs.append(
                Job(
                    source=self.name,
                    source_job_id=f"{slug}:{job_id}",
                    title=title,
                    company=_text(node, "subcompany") or slug,
                    company_slug=slug,
                    job_url=f"https://{slug}.jobs.personio.{tld}/job/{job_id}",
                    apply_url=f"https://{slug}.jobs.personio.{tld}/job/{job_id}",
                    location_raw=location_raw,
                    employment_type=employment_type,
                    department=department,
                    posted_at=_text(node, "createdAt"),
                    description_html=_descriptions(node) or None,
                    raw={
                        "seniority": _text(node, "seniority"),
                        "yearsOfExperience": _text(node, "yearsOfExperience"),
                        "keywords": _text(node, "keywords"),
                        "occupation": _text(node, "occupation"),
                        "occupationCategory": _text(node, "occupationCategory"),
                    } if with_details else {},
                )
            )
        return jobs
