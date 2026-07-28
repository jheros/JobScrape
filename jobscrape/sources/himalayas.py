"""Himalayas remote-job API.

Free, keyless, and by far the largest of the remote feeds — it reported ~96,000
indexed jobs when this adapter was written. Paginated via ``offset``/``limit``.

``locationRestrictions`` states where a remote role may hire, so it maps onto the
US-eligibility filter directly rather than being an office address.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .. import config
from ..http import Fetcher
from ..models import Job
from .base import GlobalSource, JobBatch

API = "https://himalayas.app/jobs/api"
# The API caps every response at 20 records and ignores a larger `limit`, so the
# offset stride MUST match that cap — striding by a bigger number silently skips
# the records in between.
PAGE_SIZE = 20
#: Verified to actually bind: the API was still returning full pages at page 60
#: under the previous 50-page limit, so that ceiling was silently truncating
#: real results rather than being a theoretical guard. Tunable via
#: JOBSCRAPE_HIMALAYAS_MAX_PAGES; the role/date filters trim from there.
MAX_PAGES = config.HIMALAYAS_MAX_PAGES


def _company(item: dict[str, Any]) -> str:
    """Recover the employer name.

    ``companyName`` comes back as the literal placeholder ``"name"`` for every
    record (an upstream serialization bug — ``companyLogo`` is likewise
    ``"thumbnail_url"``), so the slug is the only trustworthy source.
    """
    raw = (item.get("companyName") or "").strip()
    if raw and raw.lower() not in {"name", "companyname", "null", "none"}:
        return raw
    slug = (item.get("companySlug") or "").strip()
    if slug:
        return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-"))
    return ""


def _location(item: dict[str, Any]) -> str | None:
    """Flatten the region restrictions into a filterable string."""
    for key in ("locationRestrictions", "timezoneRestrictions"):
        value = item.get(key)
        if isinstance(value, list) and value:
            return "; ".join(str(v) for v in value if v)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # No restriction stated means it is open anywhere, which includes the US.
    return "Worldwide"


class Himalayas(GlobalSource):
    name = "himalayas"
    note = "Free remote-job API, ~96k indexed; states hiring-region restrictions."

    async def fetch(
        self,
        fetcher: Fetcher,
        since: date | None = None,
        until: date | None = None,
        queries: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        # See JobBatch — only an exhaustive crawl may be used to close postings.
        complete = True
        exhausted = False

        for page in range(MAX_PAGES):
            data = await fetcher.get_json(
                API, params={"limit": PAGE_SIZE, "offset": page * PAGE_SIZE}
            )
            if not isinstance(data, dict):
                complete = False  # the page failed; the rest is unread
                break
            batch = data.get("jobs") or []
            if not batch:
                exhausted = True
                break

            for item in batch:
                if not isinstance(item, dict):
                    continue
                job_id = str(item.get("guid") or item.get("applicationLink") or "")
                if not job_id or job_id in seen:
                    continue
                seen.add(job_id)

                categories = item.get("categories") or item.get("parentCategories") or []
                department = (
                    "; ".join(str(c) for c in categories)
                    if isinstance(categories, list) else str(categories or "") or None
                )

                def _num(value: Any) -> float | None:
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        return None

                jobs.append(
                    Job(
                        source=self.name,
                        source_job_id=job_id,
                        title=item.get("title") or "",
                        company=_company(item),
                        job_url=item.get("applicationLink") or item.get("guid") or "",
                        apply_url=item.get("applicationLink"),
                        location_raw=_location(item),
                        is_remote=True,
                        workplace_hint="remote",
                        employment_type=item.get("employmentType"),
                        department=department or None,
                        posted_at=item.get("pubDate"),
                        salary_min=_num(item.get("minSalary")),
                        salary_max=_num(item.get("maxSalary")),
                        salary_currency=item.get("currency"),
                        description_html=item.get("description")
                        or item.get("excerpt") or None,
                        raw=item,
                    )
                )
                if max_results and len(jobs) >= max_results:
                    return JobBatch(jobs, complete=False)  # capped, not exhausted

            if len(batch) < PAGE_SIZE:
                exhausted = True
                break
        return JobBatch(jobs, complete=complete and exhausted)
