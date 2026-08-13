"""Map a hiring.cafe search hit to :class:`Job`.

Field names below are verified against real live responses (see
`scraper.py`'s module docstring for how those were captured), not guessed
from documentation -- there isn't any.

Most of the useful data lives in `v5_processed_job_data`, an LLM-processed
object hiring.cafe attaches to every posting: salary is already
**annualized** (`yearly_min_compensation`/`yearly_max_compensation` --
parallel `hourly_*`/`daily_*`/`weekly_*`/`bi-weekly_*`/`monthly_*` buckets
exist too, confirming this is a normalized bucket, not a passthrough of
whatever period the posting itself used), so unlike `jsearch.py`/JobSpy this
mapper never calls `models.annualise()`.

**`description_text` is composed, not scraped.** The list endpoint carries
no prose description at all -- `job_information` has no description key in
any live sample seen. What's built here is a short summary assembled from
`v5_processed_job_data`'s own structured fields (activities, a requirements
summary, the parsed tool list, the company tagline), reusing
`jobright/scraper.py`'s `compose_description()` rather than duplicating its
bullet-list-from-structured-fields logic. It reads like a real description
but is not the original job ad text.
"""

from __future__ import annotations

from typing import Any

from ...models import Job
from ..apify import ats_link
from ..jobright.scraper import compose_description

SOURCE = "hiringcafe"

#: hiring.cafe's own closed vocabulary for `v5_processed_job_data.seniority_level`.
#: Anything not in this table is left blank, so `Job.__post_init__` derives it
#: from the title instead -- the same contract `jobright/mapper.py` documents.
_SENIORITY_MAP = {
    "No Prior Experience Required": "intern",
    "Entry Level": "entry",
    "Mid Level": "mid",
    "Senior Level": "senior",
}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _compose_description(v5: dict[str, Any]) -> str | None:
    summary = v5.get("company_tagline") or ""
    responsibilities = v5.get("role_activities") or []
    requirements: list[str] = []
    if v5.get("requirements_summary"):
        requirements.append(v5["requirements_summary"])
    tools = v5.get("technical_tools") or []
    if tools:
        requirements.append("Tech stack: " + ", ".join(tools))
    text = compose_description(summary, responsibilities, requirements, [], [])
    return text or None


def to_job(record: dict[str, Any]) -> Job | None:
    """One raw `ssrHits` entry, or ``None`` when it's not usable."""
    if not isinstance(record, dict):
        return None
    if record.get("is_expired"):
        return None

    job_id = _text(record.get("id")) or _text(record.get("objectID"))
    apply_url = _text(record.get("apply_url"))
    v5 = record.get("v5_processed_job_data") or {}
    job_info = record.get("job_information") or {}

    title = _text(job_info.get("title")) or _text(v5.get("core_job_title"))
    company = _text(v5.get("company_name"))
    if not job_id or not title or not company or not apply_url:
        return None

    board = ats_link.board_slug(apply_url)

    commitment = v5.get("commitment") or []
    employment_type = _text(commitment[0]) if commitment else None

    seniority = _SENIORITY_MAP.get(_text(v5.get("seniority_level")) or "")

    return Job(
        source=SOURCE,
        source_job_id=job_id,
        title=title,
        company=company,
        company_slug=board[1] if board else None,
        job_url=apply_url,
        apply_url=apply_url,
        location_raw=_text(v5.get("formatted_workplace_location")),
        workplace_hint=_text(v5.get("workplace_type")),
        employment_type=employment_type,
        department=_text(v5.get("job_category")),
        industry=_text(v5.get("company_sector_and_industry")),
        posted_at=_text(v5.get("estimated_publish_date")),
        salary_min=v5.get("yearly_min_compensation"),
        salary_max=v5.get("yearly_max_compensation"),
        salary_currency=_text(v5.get("listed_compensation_currency")),
        seniority=seniority,
        description_text=_compose_description(v5),
        raw=record,
    )


def to_jobs(records: list[dict[str, Any]]) -> list[Job]:
    """Map a batch, dropping records that cannot become a posting."""
    jobs = []
    for record in records:
        job = to_job(record)
        if job is not None:
            jobs.append(job)
    return jobs
