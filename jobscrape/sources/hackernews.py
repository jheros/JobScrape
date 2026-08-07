"""Hacker News "Ask HN: Who is hiring?" threads, via the Algolia API.

Top-level comments in these monthly threads are individual job posts. They are
free-form prose, so the parse is heuristic — but the threads surface startups
that never appear on an ATS aggregator, and every comment carries a real date.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..http import Fetcher
from ..models import Job, html_to_text
from .base import GlobalSource, JobBatch

SEARCH_BY_DATE = "https://hn.algolia.com/api/v1/search_by_date"
ITEMS = "https://hn.algolia.com/api/v1/items/{item_id}"

# The monthly threads are posted by the "whoishiring" account. Relevance-sorted
# search returns threads from 2015-2020, so date order is essential here.
HIRING_TITLE = re.compile(r"who\s+is\s+hiring", re.I)
# "Who wants to be hired?" is job *seekers* advertising themselves — the
# opposite of what we want, and it is posted by the same account each month.
SEEKER_TITLE = re.compile(r"wants?\s+to\s+be\s+hired", re.I)

_TITLE_RE = re.compile(r"^\s*([^|\n]{3,120}?)\s*[|｜]\s*(.+)$")
_URL_RE = re.compile(r"https?://[^\s<>\"')]+")

#: A US state code (real ones are always uppercase — "NY", "CA") or a
#: workplace word. `re.search(..., re.I)` on `[A-Z]{2}\b|remote|onsite|hybrid`
#: used to apply case-insensitivity to the WHOLE pattern, including the state
#: code alternative — turning "match an uppercase two-letter code" into
#: "match any two-letter word", which fires on ordinary lowercase words like
#: "in", "to", "on" or "is" and picks the wrong `parts[1:]` entry as the
#: location. The `(?i:...)` group scopes case-insensitivity to just the
#: workplace words, so the state-code alternative stays case-sensitive without
#: a second, separately-run pattern.
_LOCATION_HINT = re.compile(r"[A-Z]{2}\b|(?i:remote|onsite|hybrid)")

# Comments often contain several links — a blog post, the company home page,
# and the actual application link. Prefer URLs that clearly point at a job.
_JOB_URL_HINT = re.compile(
    r"(job|career|hiring|apply|position|opening|greenhouse\.io|lever\.co|"
    r"ashbyhq\.com|workable\.com|smartrecruiters\.com|myworkdayjobs\.com|"
    r"recruitee\.com|breezy\.hr|jazzhr|bamboohr|rippling|workatastartup)",
    re.I,
)


def _best_job_url(html: str) -> str | None:
    """Pick the most job-like URL in a comment, if any."""
    candidates = _URL_RE.findall(html.replace("&#x2F;", "/").replace("&amp;", "&"))
    if not candidates:
        return None
    for url in candidates:
        if _JOB_URL_HINT.search(url):
            return url.rstrip(".,;)")
    return None


class HackerNews(GlobalSource):
    name = "hackernews"
    note = "Who-is-hiring threads; free-form text, heuristic parsing."

    async def _threads(self, fetcher: Fetcher, limit: int = 4) -> list[str]:
        """Return the most recent "Who is hiring?" thread ids, newest first."""
        data = await fetcher.get_json(
            SEARCH_BY_DATE,
            params={
                "tags": "story,author_whoishiring",
                # Each month yields a hiring thread and a seeker thread, so ask
                # for extra hits before filtering the seeker ones out.
                "hitsPerPage": max(limit * 3, 12),
            },
        )
        if not isinstance(data, dict):
            return []
        threads: list[str] = []
        for hit in data.get("hits") or []:
            title = hit.get("title") or ""
            if not hit.get("objectID"):
                continue
            if SEEKER_TITLE.search(title) or not HIRING_TITLE.search(title):
                continue
            threads.append(hit["objectID"])
            if len(threads) >= limit:
                break
        return threads

    async def fetch(
        self,
        fetcher: Fetcher,
        since: date | None = None,
        until: date | None = None,
        queries: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[Job]:
        jobs: list[Job] = []
        for thread_id in await self._threads(fetcher):
            data = await fetcher.get_json(ITEMS.format(item_id=thread_id))
            if not isinstance(data, dict):
                continue
            for comment in data.get("children") or []:
                if not isinstance(comment, dict) or not comment.get("text"):
                    continue
                job = self._parse(comment)
                if job:
                    jobs.append(job)
                if max_results and len(jobs) >= max_results:
                    return JobBatch(jobs, complete=False)
        # Never complete, by construction: this reads a bounded number of recent
        # "Who is hiring?" threads, not a catalogue. It is therefore never a
        # basis for closing postings it did not return — an old thread's
        # comments stay up forever regardless of whether the role is filled, so
        # absence from this run means nothing. Liveness probing is what reaches
        # these (see jobscrape/liveness.py).
        return JobBatch(jobs, complete=False)

    def _parse(self, comment: dict[str, Any]) -> Job | None:
        html = comment.get("text") or ""
        text = html_to_text(html)
        if len(text) < 40:
            return None
        first_line = text.split("\n", 1)[0].strip()

        # The near-universal convention is "Company | Role | Location | ...".
        match = _TITLE_RE.match(first_line)
        if match:
            company = match.group(1).strip()
            remainder = match.group(2)
        else:
            company = first_line[:80]
            remainder = first_line

        parts = [p.strip() for p in remainder.split("|") if p.strip()]
        title = parts[0] if parts else first_line[:120]
        location = next(
            (p for p in parts[1:] if _LOCATION_HINT.search(p)),
            None,
        )

        hn_url = f"https://news.ycombinator.com/item?id={comment.get('id')}"
        # Fall back to the comment permalink rather than an unrelated link: the
        # comment itself always contains the real posting details.
        job_url = _best_job_url(html) or hn_url

        return Job(
            source=self.name,
            source_job_id=str(comment.get("id")),
            title=title[:200],
            company=company[:120],
            job_url=job_url,
            apply_url=job_url if job_url != hn_url else hn_url,
            location_raw=location,
            is_remote=bool(re.search(r"\bremote\b", remainder, re.I)),
            posted_at=comment.get("created_at"),
            description_html=html,
            description_text=text,
            raw=comment,
        )
