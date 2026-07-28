"""Extract schema.org ``JobPosting`` data from an HTML page.

Most ATS career pages embed a standards-compliant JSON-LD block for Google Jobs,
which is far more reliable to read than the surrounding markup: it survives
redesigns, and the field names are specified rather than invented per vendor.

Shared by every HTML-scraping adapter so the parsing quirks live in one place.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator

log = logging.getLogger("jobscrape.jsonld")

_SCRIPT_RE = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
# Fallback for pages that hand back a bare `datePosted` without valid JSON-LD.
_DATE_POSTED_RE = re.compile(r'"datePosted"\s*:\s*"([^"]+)"')
_TITLE_RE = re.compile(r'"title"\s*:\s*"([^"]{3,200})"')


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every dict in a nested JSON structure.

    JSON-LD legitimately arrives as a bare object, a list, or wrapped in an
    ``@graph``, so a recursive walk is simpler than handling each shape.
    """
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _is_job_posting(node: dict[str, Any]) -> bool:
    types = node.get("@type")
    if isinstance(types, str):
        return types == "JobPosting"
    if isinstance(types, list):
        return "JobPosting" in types
    return False


def job_postings(html: str | None) -> list[dict[str, Any]]:
    """Return every ``JobPosting`` object embedded in the page."""
    if not html:
        return []
    found: list[dict[str, Any]] = []
    for block in _SCRIPT_RE.findall(html):
        text = block.strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Some vendors emit trailing commas or unescaped newlines; a single
            # bad block should not discard the rest of the page.
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", text))
            except json.JSONDecodeError:
                continue
        found.extend(node for node in _walk(data) if _is_job_posting(node))
    return found


def first_job_posting(html: str | None) -> dict[str, Any] | None:
    postings = job_postings(html)
    return postings[0] if postings else None


def date_posted(html: str | None) -> str | None:
    """Best-effort posting date, preferring valid JSON-LD over a raw regex."""
    posting = first_job_posting(html)
    if posting and posting.get("datePosted"):
        return str(posting["datePosted"])
    if html:
        match = _DATE_POSTED_RE.search(html)
        if match:
            return match.group(1)
    return None


def location_text(posting: dict[str, Any] | None) -> str | None:
    """Flatten ``jobLocation`` (object or list) into a readable string."""
    if not posting:
        return None
    raw = posting.get("jobLocation")
    if not raw:
        # A remote-only posting may carry applicantLocationRequirements instead.
        requirement = posting.get("applicantLocationRequirements")
        entries = requirement if isinstance(requirement, list) else [requirement]
        names = [
            e.get("name") for e in entries
            if isinstance(e, dict) and e.get("name")
        ]
        return "; ".join(str(n) for n in names) or None

    entries = raw if isinstance(raw, list) else [raw]
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address")
        if isinstance(address, dict):
            country = address.get("addressCountry")
            if isinstance(country, dict):
                country = country.get("name")
            parts = [
                address.get("addressLocality"),
                address.get("addressRegion"),
                country,
            ]
            text = ", ".join(str(p) for p in parts if p)
        else:
            text = str(entry.get("name") or "")
        if text and text not in names:
            names.append(text)
    return "; ".join(names) or None


def is_remote(posting: dict[str, Any] | None) -> bool | None:
    """schema.org marks remote roles with ``jobLocationType: TELECOMMUTE``."""
    if not posting:
        return None
    if posting.get("jobLocationType") == "TELECOMMUTE":
        return True
    return None


def salary(posting: dict[str, Any] | None) -> tuple[float | None, float | None, str | None]:
    """Read ``baseSalary`` in either of its two permitted shapes."""
    if not posting:
        return None, None, None
    base = posting.get("baseSalary")
    if not isinstance(base, dict):
        return None, None, None
    currency = base.get("currency") or base.get("currencyCode")
    value = base.get("value")

    def _num(raw: Any) -> float | None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    if isinstance(value, dict):
        low = _num(value.get("minValue"))
        high = _num(value.get("maxValue"))
        if low is None and high is None:
            single = _num(value.get("value"))
            low = high = single
        return low, high, (str(currency) if currency else None)
    single = _num(value)
    return single, single, (str(currency) if currency else None)


def employment_type(posting: dict[str, Any] | None) -> str | None:
    if not posting:
        return None
    value = posting.get("employmentType")
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or None
    return str(value) if value else None


def description(posting: dict[str, Any] | None) -> str | None:
    if not posting:
        return None
    value = posting.get("description")
    return str(value) if value else None


def hiring_org(posting: dict[str, Any] | None) -> str | None:
    if not posting:
        return None
    org = posting.get("hiringOrganization")
    if isinstance(org, dict):
        return str(org.get("name") or "") or None
    return str(org) if org else None
