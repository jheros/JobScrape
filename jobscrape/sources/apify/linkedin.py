"""Map `cheap_scraper/linkedin-job-scraper` dataset items to :class:`Job`.

Looser than Indeed's — location is one free-text string and pay arrives as
prose — so this leans on derivations the project already owns rather than
parsing anything itself: `parse_us_location` splits the location,
`parse_salary` reads the pay, `classify_hint` maps the work type,
`classify_employment_type` normalises the contract type.

`experienceLevel` is deliberately **not** mapped to `seniority`. That column is
derived from the title by a precedence ladder (`jobscrape/seniority.py`) which
already resolves the cases LinkedIn's four coarse buckets get wrong — "Chief
Engineer" is an IC, "Principal Engineering Manager" is a manager — and an
adapter-supplied value would win over it, since `__post_init__` only fills a
blank. Passing the coarse value would be a downgrade dressed as extra data.
"""

from __future__ import annotations

from typing import Any

from ...models import Job
from . import ats_link

SOURCE = "linkedin"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _salary_raw(item: dict[str, Any]) -> str | None:
    """`salaryInfo` is a list of prose fragments, e.g. ``["$120,000/yr - $160,000/yr"]``.

    Joined and handed to `parse_salary` via `salary_raw`, which already reads
    that shape — including the hourly forms, which it annualises. Nothing is
    parsed here.
    """
    info = item.get("salaryInfo")
    if isinstance(info, str):
        return _text(info)
    if isinstance(info, list):
        parts = [str(p).strip() for p in info if p and isinstance(p, (str, int, float))]
        return " ".join(parts) or None
    return None


def to_job(item: dict[str, Any]) -> Job | None:
    """One dataset item, or ``None`` when it carries no usable identity.

    Every field on this actor is nullable, so identity is checked rather than
    assumed: without an id, a title, a company and a link there is nothing worth
    storing.
    """
    if not isinstance(item, dict):
        return None
    job_id = _text(item.get("jobId"))
    title = _text(item.get("jobTitle"))
    company = _text(item.get("companyName"))
    if not job_id or not title or not company:
        return None

    job_url = _text(item.get("jobUrl"))
    apply_url = _text(item.get("applyUrl"))
    if not job_url and not apply_url:
        return None

    # LinkedIn's "Easy Apply" links stay on linkedin.com and name no board; an
    # external apply link usually points straight at the employer's ATS.
    #
    # `applyType` states which it is — "EASY_APPLY" or "EXTERNAL" — so the parse
    # is skipped outright for Easy Apply rather than run against a URL that is
    # known in advance to name no board. Anything else (including a missing
    # value) still gets parsed: the field is nullable, and declining to look
    # would cost a real slug whenever it is absent.
    if _text(item.get("applyType")) == "EASY_APPLY":
        board = None
    else:
        board = ats_link.board_slug(apply_url) or ats_link.board_slug(job_url)

    return Job(
        source=SOURCE,
        source_job_id=job_id,
        title=title,
        company=company,
        company_slug=board[1] if board else None,
        job_url=job_url or apply_url,
        apply_url=apply_url or job_url,
        location_raw=_text(item.get("location")),
        # `sector` is the schema's "Industry sector" — a market ("Software
        # Development", "Hospitals and Health Care"), not the team a role sits
        # in. It used to land in `department`, which put two different facets in
        # one column. `companyIndustry` is the same facet read off the company
        # page and only exists with `enrichCompanyData: true`, so it is the
        # fallback rather than the primary.
        industry=_text(item.get("sector")) or _text(item.get("companyIndustry")),
        # Raw ATS vocabulary; `classify_employment_type` maps it to the
        # controlled one, and abstains on anything it does not recognise.
        employment_type=_text(item.get("contractType")),
        # "Remote" / "Hybrid" / "On-site" — `classify_hint` reads it by value,
        # negations first, so "Remote work is not available" cannot invert.
        workplace_hint=_text(item.get("workType")),
        posted_at=_text(item.get("publishedAt")) or _text(item.get("postedTime")),
        # The schema documents this as "Full job description (plain text)", so
        # it is passed as text. It went through `description_html` before, which
        # happened to be harmless — `html_to_text` leaves plain text alone — but
        # would have mangled any description that later arrives with markup.
        description_text=_text(item.get("jobDescription")),
        salary_raw=_salary_raw(item),
        raw=item,
    )


def to_jobs(items: list[dict[str, Any]]) -> list[Job]:
    """Map a dataset, dropping items that cannot become a posting."""
    jobs = []
    for item in items:
        job = to_job(item)
        if job is not None:
            jobs.append(job)
    return jobs
