"""Map `cheap_scraper/indeed-job-scraper` dataset items to :class:`Job`.

Indeed's actor publishes structured fields, so most of this is direct
assignment. Two places need care.

**Salary period.** The actor reports `baseSalary_min`/`baseSalary_max` alongside
a separate `salary_period` — ``"YEAR"``, ``"HOUR"``, ``"MONTH"``. Writing the
raw figure into an annual column is how 488 rows across 11 sources once came to
hold hourly rates, so every amount goes through :func:`models.annualise`, which
returns ``None`` for a period it does not recognise rather than assuming annual.
A missing salary is recoverable; a wrong one is not.

**Company slug.** Indeed's apply link usually points at the employer's real ATS
board, and reading the slug out of it is what lets `dedupe` collapse this row
against the board's own copy. See :mod:`.ats_link`.
"""

from __future__ import annotations

import re
from typing import Any

from ...models import Job, annualise
from . import ats_link

SOURCE = "indeed"

#: Employment type hides in `attributes`, an untyped list that also carries
#: benefits ("401(k)", "Health insurance") and requirements. Rather than
#: guessing which entry is the schedule, the whole list is joined and handed to
#: `classify_employment_type`, which is keyword-matched and abstains on
#: anything it does not recognise.
_ATTRIBUTE_JOIN = " | "

#: Attributes that state a work arrangement. Indeed surfaces these as chips on
#: the posting, so they are far more reliable than the same words in prose.
_WORKPLACE_ATTRIBUTES = {
    "remote": "remote",
    "fully remote": "remote",
    "work from home": "remote",
    "hybrid work": "hybrid",
    "hybrid": "hybrid",
    "in person": "onsite",
    "on-site": "onsite",
    "onsite": "onsite",
}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _location(item: dict[str, Any]) -> dict[str, Any]:
    """Indeed's nested location object, flattened.

    Returns the structured parts *and* a joined display string. Having both is
    the point: the structured fields skip `parse_us_location` entirely, and
    `location_raw` still reads sensibly on the dashboard.
    """
    loc = item.get("location")
    if not isinstance(loc, dict):
        return {"location_raw": _text(loc), "city": None, "state": None, "country": None}
    city = _text(loc.get("city"))
    state = _text(loc.get("state"))
    country = _text(loc.get("country"))
    parts = [p for p in (loc.get("streetAddress"), city, state, country) if p]
    return {
        "location_raw": ", ".join(str(p).strip() for p in parts) or None,
        "city": city,
        "state": state,
        "country": country,
    }


def _salary(item: dict[str, Any]) -> dict[str, Any]:
    """Annualised pay, or nothing.

    `annualise` returns ``None`` for an unrecognised period, and that ``None``
    is propagated rather than falling back to the raw figure — see the module
    docstring.
    """
    period = item.get("salary_period")
    low = annualise(item.get("baseSalary_min"), period)
    high = annualise(item.get("baseSalary_max"), period)
    if low is None and high is None:
        return {}
    raw_low, raw_high = item.get("baseSalary_min"), item.get("baseSalary_max")
    return {
        "salary_min": low if low is not None else high,
        "salary_max": high if high is not None else low,
        "salary_currency": _text(item.get("salary_currency")),
        # Kept so a reader can see what was published before annualisation —
        # the whole reason the 488-row bug went unnoticed was that the original
        # figure was gone.
        "salary_raw": f"{raw_low}-{raw_high} {period}".strip() if period else None,
    }


def _workplace_hint(attributes: list[str]) -> str | None:
    for attribute in attributes:
        key = str(attribute).strip().lower()
        if key in _WORKPLACE_ATTRIBUTES:
            return _WORKPLACE_ATTRIBUTES[key]
    return None


def to_job(item: dict[str, Any]) -> Job | None:
    """One dataset item, or ``None`` when it carries no usable identity."""
    if not isinstance(item, dict):
        return None
    job_id = _text(item.get("key"))
    title = _text(item.get("title"))
    if not job_id or not title:
        return None

    company_obj = item.get("company")
    company_obj = company_obj if isinstance(company_obj, dict) else {}
    company = _text(company_obj.get("companyName")) or _text(item.get("company"))
    if not company:
        return None

    job_url = _text(item.get("jobUrl"))
    apply_url = _text(item.get("applyUrl"))
    if not job_url and not apply_url:
        return None

    attributes = [a for a in (item.get("attributes") or []) if isinstance(a, str)]
    board = ats_link.board_slug(apply_url) or ats_link.board_slug(job_url)

    return Job(
        source=SOURCE,
        source_job_id=job_id,
        title=title,
        company=company,
        # The board slug when the apply link names one, so this row clusters
        # with the ATS row for the same posting. `Job.__post_init__` slugifies
        # the company name when this is left blank.
        company_slug=board[1] if board else None,
        job_url=job_url or apply_url,
        apply_url=apply_url or job_url,
        # `jobCategory` is a function ("Software Development"), so it stays in
        # `department`. The employer's market is a separate pair of fields on
        # the nested company object, and that is what `industry` wants.
        department=_text(item.get("jobCategory")),
        industry=(_text(company_obj.get("industrySector"))
                  or _text(company_obj.get("industryType"))),
        employment_type=_ATTRIBUTE_JOIN.join(attributes) or None,
        workplace_hint=_workplace_hint(attributes),
        posted_at=_text(item.get("datePublished")),
        description_html=_text(item.get("description_html")),
        description_text=_text(item.get("description_text")),
        raw=item,
        **_location(item),
        **_salary(item),
    )


def to_jobs(items: list[dict[str, Any]]) -> list[Job]:
    """Map a dataset, dropping items that cannot become a posting."""
    jobs = []
    for item in items:
        job = to_job(item)
        if job is not None:
            jobs.append(job)
    return jobs
