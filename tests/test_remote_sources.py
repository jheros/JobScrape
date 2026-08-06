"""Fixture tests for the remote-job APIs.

Each payload mirrors a real response, including the two upstream quirks that
would have cost data if left unhandled: Himalayas capping every page at 20
records regardless of `limit`, and its `companyName` arriving as the literal
placeholder string "name".
"""

from __future__ import annotations

from typing import Any

import pytest

from jobscrape.sources.himalayas import Himalayas, _company
from jobscrape.sources.jobicy import Jobicy
from jobscrape.sources.remotive import Remotive
from jobscrape.sources.weworkremotely import WeWorkRemotely, split_title


class FakeFetcher:
    """Serves queued JSON payloads and text bodies; counts requests."""

    def __init__(self, *payloads: Any, text: Any = None) -> None:
        self._payloads = list(payloads)
        self._text = text if isinstance(text, list) else ([text] if text else [])
        self.json_calls = 0
        self.text_calls = 0

    async def get_json(self, url: str, **kw: Any) -> Any:
        self.json_calls += 1
        return self._payloads.pop(0) if self._payloads else None

    async def get_text(self, url: str, **kw: Any) -> Any:
        self.text_calls += 1
        return self._text.pop(0) if self._text else None


# --- Remotive -------------------------------------------------------------

async def test_remotive_parsing() -> None:
    payload = {
        "0-legal-notice": "Please do not redistribute wholesale.",
        "jobs": [
            {
                "id": 2090903,
                "title": "Senior Backend Engineer",
                "company_name": "Acme",
                "url": "https://remotive.com/remote-jobs/acme-backend",
                "candidate_required_location": "USA",
                "publication_date": "2026-07-14T10:00:00",
                "job_type": "full_time",
                "category": "Software Development",
                "salary": "$120k - $160k",
                "description": "<p>Build services.</p>",
                "tags": ["python", "aws"],
            }
        ],
    }
    jobs = await Remotive().fetch(FakeFetcher(payload))
    assert len(jobs) == 1
    job = jobs[0]
    assert job.location_raw == "USA"          # hiring region, not an office
    assert job.workplace_type == "remote"
    assert job.posted_at is not None and job.posted_at.startswith("2026-07-14")
    assert "Build services" in job.description_text


async def test_remotive_makes_exactly_one_request() -> None:
    """The API ignores `category`, so a per-category sweep is pure waste.

    Six category slugs each returned the identical 35 records when verified.
    """
    fetcher = FakeFetcher({"jobs": []})
    await Remotive().fetch(fetcher)
    assert fetcher.json_calls == 1


# --- Himalayas ------------------------------------------------------------

def test_himalayas_company_placeholder_is_recovered_from_slug() -> None:
    """`companyName` is literally "name" for every record (upstream bug)."""
    assert _company({"companyName": "name",
                     "companySlug": "bright-vision-technologies"}) == "Bright Vision Technologies"
    # A real name is still preferred when present.
    assert _company({"companyName": "Acme Inc", "companySlug": "acme"}) == "Acme Inc"
    assert _company({"companyName": "", "companySlug": ""}) == ""


async def test_himalayas_parsing() -> None:
    page = {
        "totalCount": 96252,
        "jobs": [
            {
                "guid": "https://himalayas.app/jobs/acme-swe",
                "title": "AWS Solutions Architect",
                "companyName": "name",
                "companySlug": "bright-vision-technologies",
                "applicationLink": "https://himalayas.app/jobs/acme-swe",
                "locationRestrictions": ["United States"],
                "employmentType": "Full Time",
                "minSalary": 100000, "maxSalary": 150000, "currency": "USD",
                "pubDate": "2026-07-29T00:00:00Z",
                "description": "<p>Architect things.</p>",
                "categories": ["Cloud-Engineer"],
            }
        ],
    }
    jobs = await Himalayas().fetch(FakeFetcher(page))
    assert len(jobs) == 1
    job = jobs[0]
    assert job.company == "Bright Vision Technologies"
    assert job.location_raw == "United States"
    assert job.salary_min == 100_000 and job.salary_currency == "USD"
    assert job.workplace_type == "remote"


async def test_himalayas_unrestricted_role_is_worldwide() -> None:
    """No stated restriction means it can hire anywhere, the US included."""
    page = {"jobs": [{"guid": "g1", "title": "SWE", "companySlug": "acme",
                      "applicationLink": "https://x/1"}]}
    jobs = await Himalayas().fetch(FakeFetcher(page))
    assert jobs[0].location_raw == "Worldwide"


async def test_himalayas_timezone_restrictions_never_become_the_location() -> None:
    """`timezoneRestrictions` is a list of raw UTC-offset numbers, never a
    place name — real data was the full "-11" through "14" range (every
    valid offset, i.e. no real restriction at all) written verbatim into
    `location_raw`, which is exactly what a job with NO `locationRestrictions`
    should read as "Worldwide", not a wall of numbers."""
    page = {"jobs": [{
        "guid": "g1", "title": "SWE", "companySlug": "acme",
        "applicationLink": "https://x/1",
        "timezoneRestrictions": ["-11", "-10", "-9.5", "9.5", "10", "14"],
    }]}
    jobs = await Himalayas().fetch(FakeFetcher(page))
    assert jobs[0].location_raw == "Worldwide"


async def test_himalayas_page_stride_matches_the_api_cap() -> None:
    """The API caps responses at 20 and ignores a larger `limit`.

    Striding the offset by more than the cap silently skips the records in
    between, which is exactly the bug this guards.
    """
    from jobscrape.sources import himalayas as mod

    assert mod.PAGE_SIZE == 20, "offset stride must equal the API's hard page cap"


# --- Jobicy ---------------------------------------------------------------

async def test_jobicy_parsing() -> None:
    payload = {
        "jobCount": 1,
        "jobs": [
            {
                "id": "149290",
                "jobTitle": "Senior Software Engineer",
                "companyName": "Toast",
                "url": "https://jobicy.com/jobs/149290-senior-swe",
                "jobGeo": "USA",
                "jobLevel": "Senior",
                "jobType": ["full-time"],
                "jobIndustry": ["Engineering"],
                "salaryMin": 150000, "salaryMax": 190000, "salaryCurrency": "USD",
                "pubDate": "2026-07-29T04:30:03+00:00",
                "jobDescription": "<p>Full stack work.</p>",
            }
        ],
    }
    jobs = await Jobicy().fetch(FakeFetcher(payload))
    assert len(jobs) == 1
    job = jobs[0]
    assert job.company == "Toast"
    assert job.location_raw == "USA"
    assert job.employment_type == "full_time"   # list flattened, then normalized
    assert job.salary_max == 190_000
    assert job.workplace_type == "remote"


# --- We Work Remotely (RSS) -----------------------------------------------

def test_wwr_title_split() -> None:
    assert split_title("Reveleer: Full Stack AI Engineer") == ("Reveleer", "Full Stack AI Engineer")
    # No separator: keep everything as the title rather than mangle the company.
    assert split_title("Full Stack Engineer") == ("", "Full Stack Engineer")
    assert split_title(None) == ("", "")


WWR_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Reveleer: Full Stack AI Engineer</title>
    <region>USA Only</region>
    <category>Back-End Programming</category>
    <pubDate>Thu, 02 Apr 2026 20:46:00 +0000</pubDate>
    <link>https://weworkremotely.com/remote-jobs/reveleer-full-stack</link>
    <description>&lt;p&gt;Build AI systems.&lt;/p&gt;</description>
  </item>
</channel></rss>
"""


async def test_wwr_rss_parsing() -> None:
    jobs = await WeWorkRemotely().fetch(FakeFetcher(text=[WWR_RSS]))
    assert len(jobs) == 1
    job = jobs[0]
    assert job.company == "Reveleer"
    assert job.title == "Full Stack AI Engineer"
    assert job.location_raw == "USA Only"
    assert job.posted_at is not None and job.posted_at.startswith("2026-04-02")
    assert job.workplace_type == "remote"
    assert "Build AI systems" in job.description_text


async def test_wwr_malformed_feed_is_not_fatal() -> None:
    assert await WeWorkRemotely().fetch(FakeFetcher(text=["<not-rss"])) == []


async def test_wwr_no_response_is_not_fatal() -> None:
    assert await WeWorkRemotely().fetch(FakeFetcher()) == []


# --- these are region-scoped feeds, so eligibility must still be enforced ---

@pytest.mark.parametrize(
    "region,eligible",
    [("USA Only", True), ("Anywhere in the World", True), ("Europe Only", False)],
)
def test_remote_region_strings_drive_us_eligibility(region: str, eligible: bool) -> None:
    """Remote is not enough — the feed's region field decides US eligibility."""
    from jobscrape.filters import is_us_eligible

    assert is_us_eligible(region) is eligible
