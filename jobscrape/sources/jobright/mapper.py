"""Map a JobScoutPro-flattened JobRight record to :class:`Job`.

JobScoutPro (a separate project) logs into JobRight.ai and reads its internal
search API directly, producing one flat dict per posting (see that project's
``local/fetch_jobs.py:flatten()``). It has no `source` field of its own —
everything it produces originates from JobRight, so that's the identity this
project stamps on ingest.

Most fields are direct assignment. Two follow an existing convention rather
than a new one:

**Workplace/seniority/employment type** are handed through raw
(``workModel`` as `workplace_hint`, `employmentType` as `employment_type`) —
`Job.__post_init__` already classifies both against its own vocabulary
(`classify_workplace`/`classify_employment_type`), the same as every other
mapper. `seniority` is left blank on purpose so it's derived from the title
instead: JobRight's own label ("Senior Level") doesn't share this project's
vocabulary, and letting `classify_seniority` derive it keeps it comparable
across the whole archive.

**Company slug** goes through :mod:`jobscrape.sources.apify.ats_link` the
same way Indeed/LinkedIn do — JobRight's own apply link usually points at the
employer's real ATS board, and recovering that slug is what lets `dedupe`
collapse this row against the board's own copy instead of leaving a
duplicate (see that module's docstring for the numbers).

No salary field exists anywhere in JobScoutPro's output, so it's left blank.
"""

from __future__ import annotations

from typing import Any

from ...models import Job
from ..apify import ats_link

SOURCE = "jobright"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def to_job(record: dict[str, Any]) -> Job | None:
    """One flattened JobScoutPro record, or ``None`` when it's not usable."""
    if not isinstance(record, dict):
        return None
    job_id = _text(record.get("jobId"))
    title = _text(record.get("positionName"))
    company = _text(record.get("companyName"))
    if not job_id or not title or not company:
        return None

    url = _text(record.get("url"))
    if not url:
        return None

    board = ats_link.board_slug(url)

    return Job(
        source=SOURCE,
        source_job_id=job_id,
        title=title,
        company=company,
        # The board slug when the apply link names one, so this row clusters
        # with the ATS row for the same posting. `Job.__post_init__` slugifies
        # the company name when this is left blank.
        company_slug=board[1] if board else None,
        job_url=url,
        apply_url=url,
        location_raw=_text(record.get("location")),
        is_remote=bool(record.get("remote")) if record.get("remote") is not None else None,
        workplace_hint=_text(record.get("workModel")),
        employment_type=_text(record.get("employmentType")),
        department=_text(record.get("taxonomyPath")),
        # JobRight's own "YYYY-MM-DD HH:MM:SS" — already one of the formats
        # `Job.__post_init__`'s `to_utc_iso` falls back to, no reformatting
        # needed here.
        posted_at=_text(record.get("postedAt")),
        description_text=_text(record.get("description")),
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
