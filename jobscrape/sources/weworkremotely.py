"""We Work Remotely, via their public RSS feeds.

No key and no JSON — the category feeds are RSS, parsed with the same
``ElementTree`` approach as :mod:`jobscrape.sources.personio`.

Two shape quirks matter:

* ``title`` packs company and role together as ``"Company: Role"``.
* ``region`` states where the role may hire ("Anywhere in the World",
  "USA Only"), which is the US-eligibility signal rather than an office address.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import date

from ..http import Fetcher
from ..models import Job
from .base import GlobalSource, JobBatch

log = logging.getLogger("jobscrape.weworkremotely")

FEED = "https://weworkremotely.com/categories/{category}.rss"

# Feeds worth sweeping. Design/copywriting/sales are omitted deliberately: the role
# filter would discard them and each feed costs a request.
CATEGORIES = (
    "remote-programming-jobs",
    "remote-devops-sysadmin-jobs",
    "remote-product-jobs",
    "remote-all-other-jobs",
)


def _text(node: ET.Element, tag: str) -> str | None:
    child = node.find(tag)
    if child is None:
        return None
    value = "".join(child.itertext()).strip()
    return value or None


def split_title(raw: str | None) -> tuple[str, str]:
    """Split ``"Company: Role"`` into ``(company, role)``.

    Falls back to putting everything in the title, because a mis-split would
    silently corrupt the company column for every row in the feed.
    """
    text = (raw or "").strip()
    if ": " in text:
        company, _, role = text.partition(": ")
        if company and role:
            return company.strip(), role.strip()
    return "", text


class WeWorkRemotely(GlobalSource):
    name = "weworkremotely"
    note = "Public RSS category feeds; title is 'Company: Role', region gives eligibility."

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

        for category in CATEGORIES:
            text = await fetcher.get_text(FEED.format(category=category))
            if not text or not text.lstrip().startswith("<"):
                # A whole category silently missing from this run's results.
                complete = False
                continue
            try:
                root = ET.fromstring(text)
            except ET.ParseError as exc:
                log.debug("weworkremotely/%s: malformed RSS: %s", category, exc)
                complete = False
                continue

            for item in root.findall(".//item"):
                link = _text(item, "link") or _text(item, "guid")
                if not link or link in seen:
                    continue
                seen.add(link)

                company, role = split_title(_text(item, "title"))
                region = _text(item, "region")

                jobs.append(
                    Job(
                        source=self.name,
                        source_job_id=link,
                        title=role,
                        company=company,
                        job_url=link,
                        apply_url=link,
                        location_raw=region,
                        is_remote=True,
                        workplace_hint="remote",
                        department=_text(item, "category"),
                        posted_at=_text(item, "pubDate"),
                        description_html=_text(item, "description"),
                        raw={"category_feed": category, "region": region},
                    )
                )
                if max_results and len(jobs) >= max_results:
                    return JobBatch(jobs, complete=False)  # capped, not exhausted
        return JobBatch(jobs, complete=complete)
