"""Paycom public career portals.

Paycom is the long tail of US employers — banks, clinics, schools, dealerships,
municipal services — 5,136 portals in the public tenant list. Individually tiny
(1–120 openings each), collectively large.

The flow is bootstrap-then-search:

1. ``GET /v4/ats/web.php/portal/{token}/career-page`` embeds a
   ``configsFromHost`` blob holding a short-lived ``sessionJWT`` and a
   ``libConfig`` naming the API service host.
2. ``POST {service}/api/ats/job-posting-previews/search`` lists the board with
   a ``skip``/``take`` cursor and an exact ``jobPostingPreviewsCount``.
3. ``GET {service}/api/ats/job-postings/{id}`` returns the full posting,
   including a ``googleJobJson`` schema.org blob that supplies structured
   salary, employment type and country without any parsing of our own.

A previous pass concluded Paycom "gates every call behind a token that lives
inside a minified JS bundle" and dropped it for Rippling. That was wrong: the
token is in the page's own HTML, not in a bundle, and the endpoint tried then
(``/jobs/1``) was simply the wrong one. Re-measured across 12 random portals,
all 12 bootstrapped and returned live counts.

A company is addressed by its 32-hex portal token.

Credit: bootstrap shape from ats-scrapers (MIT) —
https://github.com/kalil0321/ats-scrapers
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from ..http import Fetcher, gather_limited
from ..models import Job, annualise, html_to_text
from .base import BoardSource, Prefilter, shallow_refresh

log = logging.getLogger("jobscrape.paycom")

PORTAL = "https://www.paycomonline.net/v4/ats/web.php/portal/{token}/career-page"
JOB_PAGE = "https://www.paycomonline.net/v4/ats/web.php/portal/{token}/jobs/{job_id}"
SEARCH_PATH = "/api/ats/job-posting-previews/search"
DETAIL_PATH = "/api/ats/job-postings/{job_id}"

PAGE_SIZE = 100
#: Bounds a single portal at 5,000 postings. The largest sampled had 120.
MAX_PAGES = 50

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$", re.I)
_CONFIG_RE = re.compile(r"var configsFromHost\s*=\s*(\{.*?\});\s*var Mountable", re.S)

#: ``libConfig`` names the host we then send the session JWT to, and it comes
#: out of a page we do not control. The SSRF guard stops private targets; this
#: pins the host to Paycom's own domain so a tampered portal page cannot
#: redirect an authenticated request to an unrelated public host.
_SERVICE_DOMAIN = ".paycomonline.net"

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: schema.org employment types, as Google Jobs spells them.
_EMPLOYMENT = {
    "FULL_TIME": "Full-time", "PART_TIME": "Part-time", "CONTRACTOR": "Contract",
    "TEMPORARY": "Temporary", "INTERN": "Internship", "PER_DIEM": "Per diem",
    "VOLUNTEER": "Volunteer", "OTHER": None,
}

#: Below this a figure cannot be an annual salary, so it must be a rate. Used
#: only when the payload names no period at all — see `_salary`.
_ANNUAL_FLOOR = 1000.0


def parse_slug(slug: str) -> str | None:
    """Accept a bare portal token or a full career-page URL."""
    value = slug.strip()
    if value.startswith(("http://", "https://")):
        match = re.search(r"/portal/([0-9a-f]{32})", value, re.I)
        value = match.group(1) if match else ""
    return value.lower() if _TOKEN_RE.fullmatch(value) else None


def _service_url(lib_config: dict[str, Any]) -> str | None:
    url = lib_config.get("atsPortalMantleServiceUrl")
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    host = (urlsplit(url).hostname or "").lower()
    if not (host == "paycomonline.net" or host.endswith(_SERVICE_DOMAIN)):
        return None
    return url.rstrip("/")


def _jwt_claims(token: str) -> dict[str, Any]:
    """Read a JWT payload without verifying it.

    We are not authenticating anything — the claims are read only to recover
    the employer's client code for the ``raw`` record, and the token is one the
    portal handed us to send straight back. Nothing downstream trusts it.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return {}


def _search_body(skip: int) -> dict[str, Any]:
    return {
        "skip": skip,
        "take": PAGE_SIZE,
        # Every filter list must be present, empty. Omitting them returns a 400.
        "filtersForQuery": {
            "distanceFrom": 0,
            "workEnvironments": [], "positionTypes": [], "educationLevels": [],
            "categories": [], "travelTypes": [], "shiftTypes": [], "otherFilters": [],
            "keywordSearchText": "", "location": "", "sortOption": "",
        },
    }


def _google_job(detail: dict[str, Any]) -> dict[str, Any]:
    """The schema.org JobPosting blob, shipped as a JSON *string*."""
    blob = detail.get("googleJobJson")
    if isinstance(blob, dict):
        return blob
    if isinstance(blob, str) and blob.strip():
        try:
            parsed = json.loads(blob)
        except ValueError:
            # Malformed structured metadata is an enrichment we can do without;
            # the posting itself is still perfectly usable.
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _salary(
    google: dict[str, Any], detail: dict[str, Any]
) -> tuple[float | None, float | None, str | None, str | None]:
    """Return ``(annual_min, annual_max, currency, raw_text)``.

    Paycom's long tail is mostly hourly, so the period matters more here than
    anywhere else — and it is **not** in the schema.org blob. Measured across a
    portal's 74 postings, ``baseSalary.value.unitText`` was the empty string on
    every one, while the detail's ``salaryRange`` read ``"$22.00 - $24.00
    Hourly"``. So the numbers come from the structured blob and the period from
    the prose field, with ``unitText`` preferred whenever a tenant does fill it.

    Getting this wrong is not a missing value but a wrong one: without the
    period, ``22`` lands in the same column as ``220000`` and every salary
    filter and sort silently compares the two.
    """
    salary = google.get("baseSalary")
    raw_text = detail.get("salaryRange") if isinstance(detail.get("salaryRange"), str) else None
    if not isinstance(salary, dict):
        return None, None, None, raw_text
    value = salary.get("value")
    if not isinstance(value, dict):
        return None, None, None, raw_text

    def _num(raw: Any) -> float | None:
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    low = _num(value.get("minValue")) or _num(value.get("value"))
    high = _num(value.get("maxValue")) or low
    if low is None:
        return None, None, None, raw_text

    unit = str(value.get("unitText") or "").upper()
    if not unit and raw_text:
        # "$22.00 - $24.00 Hourly" — the trailing word is the period.
        match = re.search(
            r"\b(hourly|hour|daily|day|weekly|week|biweekly|monthly|month|"
            r"yearly|year|annual|annually)\b", raw_text, re.I)
        unit = match.group(1).upper() if match else ""
    scaled_low = annualise(low, unit)
    if scaled_low is None:
        # Some tenants write "$60,000.00 - $175,000.00 Salary", naming no
        # period at all. Magnitude settles it without guessing: the two
        # populations do not overlap, since no US annual salary is under
        # $1,000 and no hourly rate reaches it.
        unit = "year" if low >= _ANNUAL_FLOOR else "hour"
        scaled_low = annualise(low, unit)
    scaled_high = annualise(high, unit) if high is not None else None

    return scaled_low, scaled_high, salary.get("currency") or None, raw_text


def _country(google: dict[str, Any]) -> str | None:
    location = google.get("jobLocation")
    if isinstance(location, list):
        location = location[0] if location else None
    if not isinstance(location, dict):
        return None
    address = location.get("address")
    if isinstance(address, dict):
        country = address.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        if isinstance(country, str) and country.strip():
            return country.strip()
    return None


class Paycom(BoardSource):
    name = "paycom"
    ats = "paycom"
    note = "Session JWT bootstrapped from the portal page; detail has schema.org salary."

    async def _bootstrap(
        self, fetcher: Fetcher, token: str
    ) -> tuple[str, dict[str, str], str | None] | None:
        """Return ``(service_url, api_headers, client_code)``."""
        portal_url = PORTAL.format(token=token)
        page = await fetcher.get_text(
            portal_url,
            headers={"User-Agent": _BROWSER_UA,
                     "Accept": "text/html,application/xhtml+xml,application/json"},
        )
        if not page:
            return None
        match = _CONFIG_RE.search(page)
        if not match:
            return None
        try:
            # The blob is HTML-escaped inside the script tag on some portals.
            config = json.loads(html.unescape(match.group(1)))
            lib_config = json.loads(config["libConfig"])
        except (ValueError, KeyError, TypeError):
            return None
        session_jwt = config.get("sessionJWT")
        if not isinstance(session_jwt, str) or not session_jwt:
            return None
        if not isinstance(lib_config, dict):
            return None
        service = _service_url(lib_config)
        if service is None:
            log.warning("Paycom portal %s named an off-domain service host", token)
            return None
        headers = {
            # Sent bare, with no "Bearer" prefix — the API rejects it prefixed.
            "Authorization": session_jwt,
            "Locale": str(lib_config.get("locale") or "en-US"),
            "Translation-Highlights": str(bool(lib_config.get("translationHighlights"))).lower(),
            "Portal-Host-Referrer": portal_url,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _BROWSER_UA,
        }
        return service, headers, _jwt_claims(session_jwt).get("clientcode")

    async def _previews(
        self, fetcher: Fetcher, service: str, headers: dict[str, str], paginate: bool
    ) -> list[dict[str, Any]] | None:
        rows: list[dict[str, Any]] = []
        total: int | None = None
        for _ in range(MAX_PAGES):
            data = await fetcher.post_json(
                f"{service}{SEARCH_PATH}", _search_body(len(rows)), headers=headers
            )
            if not isinstance(data, dict):
                return rows or None
            if total is None:
                count = data.get("jobPostingPreviewsCount")
                total = count if isinstance(count, int) and count >= 0 else None
            page = data.get("jobPostingPreviews")
            if not isinstance(page, list) or not page:
                break
            rows.extend(p for p in page if isinstance(p, dict))
            if not paginate or (total is not None and len(rows) >= total):
                break
        return rows

    async def _detail(
        self, fetcher: Fetcher, service: str, headers: dict[str, str], job_id: str
    ) -> dict[str, Any] | None:
        data = await fetcher.get_json(
            f"{service}{DETAIL_PATH.format(job_id=job_id)}", headers=headers
        )
        if isinstance(data, dict) and isinstance(data.get("jobPosting"), dict):
            return data["jobPosting"]
        return None

    async def fetch_board(
        self,
        fetcher: Fetcher,
        slug: str,
        with_details: bool = True,
        prefilter: Prefilter | None = None,
        known_ids: set[str] | None = None,
        **kw: Any,
    ) -> list[Job] | None:
        token = parse_slug(slug)
        if not token:
            return None
        session = await self._bootstrap(fetcher, token)
        if session is None:
            return None
        service, headers, client_code = session

        previews = await self._previews(fetcher, service, headers, paginate=with_details)
        if previews is None:
            # Bootstrapped but the search refused. `None`, not `[]`: an empty
            # list marks the board successful and resets its health counter, so
            # a portal whose API always refuses would look healthy forever while
            # yielding nothing. `None` backs off and still closes no postings.
            return None
        # One posting can be listed once per location, so dedupe by id.
        rows: dict[str, dict[str, Any]] = {}
        for item in previews:
            job_id = item.get("jobId")
            if job_id is not None:
                rows.setdefault(str(job_id), item)
        if not rows:
            return []

        if prefilter is not None:
            rows = {
                job_id: item for job_id, item in rows.items()
                if prefilter(item.get("jobTitle"), item.get("category"),
                             item.get("locations"))
            }
            if not rows:
                return []

        known = known_ids or set()
        details: dict[str, dict[str, Any]] = {}
        if with_details:
            needed = [j for j in rows if f"{token}:{j}" not in known]
            fetched = await gather_limited(
                [self._detail(fetcher, service, headers, j) for j in needed], limit=4
            )
            for job_id, result in zip(needed, fetched):
                if isinstance(result, dict):
                    details[job_id] = result

        jobs: list[Job] = []
        for job_id, item in rows.items():
            detail = details.get(job_id, {})
            google = _google_job(detail)
            low, high, currency, salary_text = _salary(google, detail)
            body = detail.get("jobDescription") or google.get("description") \
                or item.get("description")
            remote = str(item.get("remoteType") or detail.get("remoteType") or "")
            job = Job(
                source=self.name,
                source_job_id=f"{token}:{job_id}",
                title=(item.get("jobTitle") or detail.get("jobTitle")
                       or google.get("title") or ""),
                # The portal token is opaque, so without a hiringOrganization
                # the company would be a 32-hex string. Falling back to the
                # token keeps the row addressable; the name usually arrives
                # from the tenant list at import time.
                company=str((google.get("hiringOrganization") or {}).get("name")
                            or detail.get("clientName") or token),
                company_slug=token,
                job_url=JOB_PAGE.format(token=token, job_id=job_id),
                apply_url=JOB_PAGE.format(token=token, job_id=job_id),
                location_raw=item.get("locations") or detail.get("locations"),
                country=_country(google),
                is_remote=True if remote.upper().startswith("REMOTE") else None,
                workplace_hint=remote or None,
                employment_type=_EMPLOYMENT.get(
                    str(google.get("employmentType") or "").upper()
                ) or item.get("positionType"),
                department=item.get("category") or detail.get("category"),
                posted_at=google.get("datePosted") or detail.get("postDate"),
                salary_min=low,
                salary_max=high,
                salary_currency=currency,
                salary_raw=salary_text,
                description_html=body,
                description_text=html_to_text(body) or None,
                raw={**detail, "clientCode": client_code} if detail else item,
            )
            if job.source_job_id in known:
                job = shallow_refresh(job)
            jobs.append(job)
        return jobs

    async def probe(self, fetcher: Fetcher, slug: str) -> tuple[str, int]:
        """Bootstrap plus one search page; never the detail fan-out."""
        jobs = await self.fetch_board(fetcher, slug, with_details=False)
        if jobs is None:
            return "dead", 0
        return ("live", len(jobs)) if jobs else ("empty", 0)
