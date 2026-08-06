"""Fixture tests for the platforms added during the coverage expansion.

Payloads mirror what each provider actually returned when the adapters were
written, including the shape quirks that caused real bugs: Breezy returning
objects where other boards return strings, Recruitee's non-ISO timestamps, and
Pinpoint shipping no posting date in its JSON at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobscrape.sources import jsonld
from jobscrape.sources.bamboohr import BambooHR
from jobscrape.sources.breezy import Breezy
from jobscrape.sources.personio import Personio
from jobscrape.sources.pinpoint import Pinpoint
from jobscrape.sources.recruitee import Recruitee
from jobscrape.sources.teamtailor import Teamtailor


class FakeFetcher:
    """Serves queued payloads in order; `text` responses are served separately."""

    def __init__(self, *payloads: Any, text: Any = None) -> None:
        self._payloads = list(payloads)
        self._text = text if isinstance(text, list) else ([text] if text else [])

    async def get_json(self, url: str, **kw: Any) -> Any:
        return self._payloads.pop(0) if self._payloads else None

    async def post_json(self, url: str, body: Any, **kw: Any) -> Any:
        return self._payloads.pop(0) if self._payloads else None

    async def get_text(self, url: str, **kw: Any) -> Any:
        return self._text.pop(0) if self._text else None


# --- Recruitee ------------------------------------------------------------

async def test_recruitee_parsing_and_workplace_flags() -> None:
    payload = {
        "offers": [
            {
                "id": 12345,
                "title": "Senior DevOps Engineer",
                "company_name": "11 bit studios",
                "careers_url": "https://11bitstudios.recruitee.com/o/senior-devops",
                "careers_apply_url": "https://11bitstudios.recruitee.com/o/senior-devops/c/new",
                "city": "Warsaw", "state_code": None,
                "country_code": "pl",
                "locations": [{"name": "Warsaw, Poland"}],
                "remote": False, "hybrid": True, "on_site": False,
                "department": "Technology",
                "employment_type_code": "permanent",
                # Not ISO-8601: a trailing zone *name*, which broke date parsing.
                "created_at": "2026-07-16 13:23:08 UTC",
                "published_at": "2026-07-17 08:18:32 UTC",
                "updated_at": "2026-07-22 10:55:58 UTC",
                "description": "<p>Run the pipelines.</p>",
                "requirements": "<p>Kubernetes.</p>",
            }
        ]
    }
    jobs = await Recruitee().fetch_board(FakeFetcher(payload), "11bitstudios")
    assert jobs is not None and len(jobs) == 1
    job = jobs[0]
    assert job.posted_at is not None and job.posted_at.startswith("2026-07-17")
    assert job.workplace_type == "hybrid", "hybrid flag must win over remote/on_site"
    assert job.country == "PL"
    assert "Run the pipelines" in job.description_text
    assert "Kubernetes" in job.description_text


async def test_recruitee_missing_board() -> None:
    assert await Recruitee().fetch_board(FakeFetcher(None), "nope") is None


# --- Pinpoint -------------------------------------------------------------

async def test_pinpoint_takes_date_from_page_json_ld() -> None:
    """Pinpoint's JSON has no posting date; without the page scrape every row
    would be undated and dropped by any --since window."""
    payload = {
        "data": [
            {
                "id": "500888",
                "title": "Platform Engineer",
                "url": "https://acme.pinpointhq.com/en/postings/abc",
                "location": {"city": "Austin", "province": "TX"},
                "workplace_type": "remote",
                "employment_type_text": "Full time",
                "job": {"department": {"name": "Engineering"}},
                "description": "<p>Build platforms.</p>",
                "compensation_minimum": "120000",
                "compensation_maximum": "160000",
                "compensation_currency": "USD",
            }
        ]
    }
    page = '<script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-05-01T09:04:58+01:00"}</script>'
    jobs = await Pinpoint().fetch_board(FakeFetcher(payload, text=page), "acme")
    assert jobs is not None and len(jobs) == 1
    job = jobs[0]
    assert job.posted_at is not None and job.posted_at.startswith("2026-05-01")
    assert job.workplace_type == "remote"
    assert job.salary_min == 120_000 and job.salary_max == 160_000
    assert job.department == "Engineering"


# --- Teamtailor -----------------------------------------------------------

async def test_teamtailor_json_feed_with_embedded_schema() -> None:
    payload = {
        "title": "Acme Careers",
        "items": [
            {
                "id": "8142497",
                "title": "Backend Engineer",
                "url": "https://acme.teamtailor.com/jobs/8142497-backend",
                "date_published": "2026-07-29T11:37:47+02:00",
                "content_html": "<p>Write services.</p>",
                "_jobposting": {
                    "@type": "JobPosting",
                    "employmentType": "FULL_TIME",
                    "jobLocationType": "TELECOMMUTE",
                    "hiringOrganization": {"name": "Acme Inc"},
                    "jobLocation": {
                        "address": {
                            "addressLocality": "Austin",
                            "addressRegion": "TX",
                            "addressCountry": "US",
                        }
                    },
                },
            }
        ],
    }
    jobs = await Teamtailor().fetch_board(FakeFetcher(payload), "acme")
    assert jobs is not None and len(jobs) == 1
    job = jobs[0]
    assert job.company == "Acme Inc"
    assert job.posted_at is not None and job.posted_at.startswith("2026-07-29")
    assert job.location_raw == "Austin, TX, US"
    assert job.workplace_type == "remote"  # from TELECOMMUTE
    assert "Write services" in job.description_text


# --- Personio (XML) -------------------------------------------------------

PERSONIO_XML = """<?xml version="1.0" encoding="utf-8"?>
<workzag-jobs>
  <position>
    <id>2687536</id>
    <subcompany>Acme GmbH</subcompany>
    <office>Austin</office>
    <additionalOffices>       </additionalOffices>
    <department>Engineering</department>
    <name>Backend Engineer (m/w/d)</name>
    <jobDescriptions>
      <jobDescription><name>Your mission</name><value>&lt;p&gt;Build APIs.&lt;/p&gt;</value></jobDescription>
      <jobDescription><name>Your profile</name><value>&lt;p&gt;Python.&lt;/p&gt;</value></jobDescription>
    </jobDescriptions>
    <employmentType>permanent</employmentType>
    <schedule>full-time</schedule>
    <seniority>entry-level</seniority>
    <createdAt>2026-06-25T17:23:34+00:00</createdAt>
  </position>
</workzag-jobs>
"""


async def test_personio_xml_parsing() -> None:
    jobs = await Personio().fetch_board(FakeFetcher(text=PERSONIO_XML), "acme")
    assert jobs is not None and len(jobs) == 1
    job = jobs[0]
    assert job.title == "Backend Engineer (m/w/d)"
    assert job.company == "Acme GmbH"
    # Whitespace-only additionalOffices must not become part of the location.
    assert job.location_raw == "Austin"
    assert job.posted_at is not None and job.posted_at.startswith("2026-06-25")
    assert "Build APIs" in job.description_text
    assert "Your profile" in job.description_text
    assert job.employment_type == "full_time"   # normalized from "permanent, full-time"


async def test_personio_malformed_xml_is_not_fatal() -> None:
    assert await Personio().fetch_board(FakeFetcher(text="<not-xml"), "acme") is None


async def test_personio_missing_feed() -> None:
    assert await Personio().fetch_board(FakeFetcher(), "acme") is None


# --- BambooHR -------------------------------------------------------------

async def test_bamboohr_list_plus_detail() -> None:
    listing = {
        "meta": {"totalCount": 1},
        "result": [
            {
                "id": "26",
                "jobOpeningName": "Software Engineer",
                "departmentLabel": "Engineering",
                "employmentStatusLabel": "Permanent",
                "location": {"city": "Austin", "state": "Texas"},
                "isRemote": True,
                "locationType": "remote",
            }
        ],
    }
    detail = {
        "meta": {},
        "result": {
            "jobOpening": {
                "jobOpeningShareUrl": "https://acme.bamboohr.com/careers/26",
                "jobOpeningName": "Software Engineer",
                "description": "<p>Write code.</p>",
                "datePosted": "2026-04-26",
            }
        },
    }
    jobs = await BambooHR().fetch_board(FakeFetcher(listing, detail), "acme")
    assert jobs is not None and len(jobs) == 1
    job = jobs[0]
    assert job.source_job_id == "acme:26"  # scoped: ids are per-account
    assert job.location_raw == "Austin, Texas"
    assert job.workplace_type == "remote"
    assert job.posted_at is not None and job.posted_at.startswith("2026-04-26")
    assert "Write code" in job.description_text


# --- Breezy ---------------------------------------------------------------

async def test_breezy_handles_object_valued_fields() -> None:
    """Breezy returns objects where other boards return plain strings.

    `company` as a dict previously raised AttributeError during normalization.
    """
    listing = [
        {
            "id": "e7a1fed92878",
            "name": "Contract Grant Writer",
            "url": "https://acme.breezy.hr/p/e7a1fed92878-writer",
            "published_date": "2026-06-26T13:51:03.752Z",
            "type": {"id": "contract", "name": "Contract"},
            "location": {
                "country": {"name": "Canada", "id": "CA"},
                "state": {"id": "ON", "name": "Ontario"},
                "city": "Toronto",
                "is_remote": True,
            },
            "department": "Innovation Funding",
            "salary": "$35 - $75 / hour",
            "company": {"name": "RightBlue Labs Inc", "friendly_id": "acme"},
        }
    ]
    page = ('<script type="application/ld+json">{"@type":"JobPosting",'
            '"description":"<p>Write grants.</p>"}</script>')
    jobs = await Breezy().fetch_board(FakeFetcher(listing, text=page), "acme")
    assert jobs is not None and len(jobs) == 1
    job = jobs[0]
    assert job.company == "RightBlue Labs Inc"  # unwrapped from the dict
    assert job.employment_type == "contract"    # normalized from "Contract"
    assert job.country == "CA"
    assert job.workplace_type == "remote"
    assert job.posted_at is not None and job.posted_at.startswith("2026-06-26")
    assert "Write grants" in job.description_text
    assert job.source_job_id == "acme:e7a1fed92878"


async def test_breezy_worldwide_sentinel_is_not_a_place() -> None:
    """Breezy's own sentinel for "open to anywhere" is `city: "ww"` paired
    with `country: {id: "worldwide"}` — real values in the payload, but
    neither is an actual place; both used to be stored verbatim."""
    listing = [
        {
            "id": "abc123",
            "name": "Remote Support Engineer",
            "url": "https://acme.breezy.hr/p/abc123-support",
            "published_date": "2026-06-26T13:51:03.752Z",
            "location": {
                "country": {"name": "Worldwide", "id": "worldwide"},
                "city": "ww",
                "is_remote": True,
            },
            "company": {"name": "Acme", "friendly_id": "acme"},
        }
    ]
    page = ('<script type="application/ld+json">{"@type":"JobPosting",'
            '"description":"<p>Support things.</p>"}</script>')
    jobs = await Breezy().fetch_board(FakeFetcher(listing, text=page), "acme")
    assert jobs is not None and len(jobs) == 1
    job = jobs[0]
    assert job.city is None
    assert job.country is None


# --- shared JSON-LD helper ------------------------------------------------

def test_jsonld_finds_posting_in_graph() -> None:
    html = ('<script type="application/ld+json">'
            '{"@context":"http://schema.org","@graph":['
            '{"@type":"Organization","name":"Acme"},'
            '{"@type":"JobPosting","title":"SWE","datePosted":"2026-07-01"}]}'
            '</script>')
    posting = jsonld.first_job_posting(html)
    assert posting is not None and posting["title"] == "SWE"
    assert jsonld.date_posted(html) == "2026-07-01"


def test_jsonld_tolerates_trailing_commas() -> None:
    html = ('<script type="application/ld+json">'
            '{"@type":"JobPosting","title":"SWE",}</script>')
    assert jsonld.first_job_posting(html) is not None


def test_jsonld_falls_back_to_regex_for_date() -> None:
    """Invalid JSON-LD should not cost us the date if it is findable."""
    assert jsonld.date_posted('garbage "datePosted": "2026-03-03" more') == "2026-03-03"


def test_jsonld_salary_shapes() -> None:
    ranged = {"baseSalary": {"currency": "USD",
                             "value": {"minValue": 100, "maxValue": 200}}}
    assert jsonld.salary(ranged) == (100.0, 200.0, "USD")
    single = {"baseSalary": {"currency": "USD", "value": 150}}
    assert jsonld.salary(single) == (150.0, 150.0, "USD")
    assert jsonld.salary({}) == (None, None, None)


def test_jsonld_location_and_remote() -> None:
    posting = {
        "jobLocationType": "TELECOMMUTE",
        "jobLocation": [
            {"address": {"addressLocality": "Austin", "addressRegion": "TX"}},
            {"address": {"addressLocality": "Denver", "addressRegion": "CO"}},
        ],
    }
    assert jsonld.location_text(posting) == "Austin, TX; Denver, CO"
    assert jsonld.is_remote(posting) is True


def test_jsonld_remote_only_uses_applicant_requirements() -> None:
    posting = {"applicantLocationRequirements": {"@type": "Country", "name": "USA"}}
    assert jsonld.location_text(posting) == "USA"


def test_jsonld_empty_input() -> None:
    assert jsonld.job_postings(None) == []
    assert jsonld.job_postings("<html></html>") == []
    assert jsonld.first_job_posting("") is None


# --- JazzHR (HTML + JSON-LD) ----------------------------------------------

JAZZHR_LIST = '''
<html><body>
<a href="https://acme.applytojob.com/apply/LgHWUlCzcA/Backend-Engineer">Backend Engineer</a>
<a href="https://acme.applytojob.com/apply/qVG7M47JuB/Mechanical-Engineer">Mechanical Engineer</a>
</body></html>
'''

JAZZHR_POSTING = '''
<script type="application/ld+json">{
 "@context":"http://schema.org","@type":"JobPosting",
 "title":"Backend Engineer","datePosted":"2026-05-05",
 "employmentType":"FULL_TIME",
 "hiringOrganization":{"name":"Acme Inc"},
 "jobLocation":{"address":{"addressLocality":"Everett","addressRegion":"MA"}},
 "description":"<p>Build the backend.</p>",
 "baseSalary":{"currency":"USD","value":{"minValue":130000,"maxValue":170000}}
}</script>
'''


async def test_jazzhr_extracts_links_then_reads_json_ld() -> None:
    from jobscrape.sources.jazzhr import JazzHR

    fetcher = FakeFetcher(text=[JAZZHR_LIST, JAZZHR_POSTING, JAZZHR_POSTING])
    jobs = await JazzHR().fetch_board(fetcher, "acme")
    assert jobs is not None and len(jobs) == 2
    job = jobs[0]
    assert job.title == "Backend Engineer"
    assert job.company == "Acme Inc"
    assert job.location_raw == "Everett, MA"
    assert job.posted_at is not None and job.posted_at.startswith("2026-05-05")
    assert job.salary_min == 130_000 and job.salary_currency == "USD"
    assert "Build the backend" in job.description_text
    assert job.source_job_id == "acme:LgHWUlCzcA"


async def test_jazzhr_prefilter_skips_before_detail_fetch() -> None:
    """The URL slug is enough to skip an obviously irrelevant posting.

    Only one posting page is queued, so if the prefilter failed to drop
    "Mechanical-Engineer" the second detail fetch would come back empty.
    """
    from jobscrape.filters import cheap_prefilter
    from jobscrape.sources.jazzhr import JazzHR

    fetcher = FakeFetcher(text=[JAZZHR_LIST, JAZZHR_POSTING])
    jobs = await JazzHR().fetch_board(fetcher, "acme", prefilter=cheap_prefilter)
    assert jobs is not None and len(jobs) == 1
    assert jobs[0].title == "Backend Engineer"


async def test_jazzhr_missing_board_returns_none() -> None:
    from jobscrape.sources.jazzhr import JazzHR

    assert await JazzHR().fetch_board(FakeFetcher(text=None), "nope") is None


async def test_jazzhr_board_with_no_openings_returns_empty() -> None:
    from jobscrape.sources.jazzhr import JazzHR

    empty = '<html><body>applytojob.com no openings</body></html>'
    assert await JazzHR().fetch_board(FakeFetcher(text=empty), "acme") == []


# --- Ashby: itemised compensation -----------------------------------------
#
# Ashby breaks pay into typed components. Across five live boards they were
# 219 Salary, 132 EquityCashValue, 81 EquityPercentage, 48 Commission, 1 Bonus
# — and the human summary concatenates them ("$211.4K – $290.6K • Offers
# Equity"), so reading a range out of that text can pick up an equity valuation
# and file it as pay.


def _ashby_compensation(*components: dict[str, Any], summary: str = "") -> dict[str, Any]:
    return {
        "compensationTierSummary": summary,
        "compensationTiers": [{"id": "t1", "tierSummary": summary,
                               "components": list(components)}],
    }


def _salary_component(min_v: float, max_v: float, interval: str = "1 YEAR") -> dict[str, Any]:
    return {"compensationType": "Salary", "interval": interval,
            "currencyCode": "USD", "minValue": min_v, "maxValue": max_v,
            "summary": f"${min_v} – ${max_v}"}


def test_ashby_reads_the_exact_band_from_components() -> None:
    from jobscrape.sources.ashby import _salary_from_components

    comp = _ashby_compensation(_salary_component(211_400, 290_600),
                               summary="$211.4K – $290.6K • Offers Equity")
    assert _salary_from_components(comp) == (211_400, 290_600, "USD")


def test_ashby_ignores_equity_commission_and_bonus() -> None:
    """An equity cash value beside a salary must not be read as pay."""
    from jobscrape.sources.ashby import _salary_from_components

    equity_first = _ashby_compensation(
        {"compensationType": "EquityCashValue", "interval": "1 YEAR",
         "currencyCode": "USD", "minValue": 900_000, "maxValue": 900_000},
        {"compensationType": "Commission", "interval": "1 YEAR",
         "currencyCode": "USD", "minValue": 50_000, "maxValue": 50_000},
        _salary_component(150_000, 180_000),
    )
    assert _salary_from_components(equity_first) == (150_000, 180_000, "USD")

    # Equity alone is not a salary.
    equity_only = _ashby_compensation(
        {"compensationType": "EquityPercentage", "interval": "NONE",
         "currencyCode": None, "minValue": None, "maxValue": None},
    )
    assert _salary_from_components(equity_only) == (None, None, None)


def test_ashby_annualises_a_monthly_interval() -> None:
    """218 of 219 components measured were "1 YEAR"; one was "1 MONTH"."""
    from jobscrape.sources.ashby import _salary_from_components

    comp = _ashby_compensation(_salary_component(5_000, 6_000, interval="1 MONTH"))
    assert _salary_from_components(comp) == (60_000, 72_000, "USD")


def test_ashby_handles_a_single_value_band() -> None:
    """"$80K" is one number, and the summary regex needs a *range* — so this
    kind of posting previously carried no salary at all."""
    from jobscrape.sources.ashby import _salary_from_components
    from jobscrape.models import parse_salary

    assert parse_salary("$80K • Offers Equity")[0] is None, "the regex needs two amounts"
    comp = _ashby_compensation(
        {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD",
         "minValue": 80_000, "maxValue": None})
    assert _salary_from_components(comp) == (80_000, 80_000, "USD")


def test_ashby_missing_or_malformed_compensation_is_survivable() -> None:
    from jobscrape.sources.ashby import _salary_from_components

    assert _salary_from_components({}) == (None, None, None)
    assert _salary_from_components({"compensationTiers": "nonsense"}) == (None, None, None)
    assert _salary_from_components({"compensationTiers": [None, {"components": [None]}]}) \
        == (None, None, None)


# --- Pinpoint: pay period and visibility -----------------------------------


def _pinpoint_posting(**over: Any) -> dict[str, Any]:
    item = {
        "id": 1, "title": "Support Specialist",
        "url": "https://acme.pinpointhq.com/en/postings/1",
        "location": {"city": "Annapolis", "province": "MD"},
        "description": "<p>Help members.</p>",
        "compensation": "$16.00 - $18.00 / hour",
        "compensation_minimum": 16.0, "compensation_maximum": 18.0,
        "compensation_currency": "USD", "compensation_frequency": "hour",
        "compensation_visible": True,
    }
    item.update(over)
    return item


@pytest.mark.asyncio
async def test_pinpoint_annualises_by_the_stated_frequency() -> None:
    """201 of 323 postings with visible pay across 39 boards were hourly."""
    fetcher = FakeFetcher({"data": [_pinpoint_posting()]})
    jobs = await Pinpoint().fetch_board(fetcher, "acme", with_details=False)
    assert jobs[0].salary_min == 16 * 2080
    assert jobs[0].salary_max == 18 * 2080
    # The published text is what the numbers were derived from.
    assert jobs[0].salary_raw == "$16.00 - $18.00 / hour"


@pytest.mark.asyncio
async def test_pinpoint_leaves_annual_pay_alone() -> None:
    fetcher = FakeFetcher({"data": [_pinpoint_posting(
        compensation="$200,000 - $240,000 / year", compensation_minimum=200_000,
        compensation_maximum=240_000, compensation_frequency="year")]})
    jobs = await Pinpoint().fetch_board(fetcher, "acme", with_details=False)
    assert (jobs[0].salary_min, jobs[0].salary_max) == (200_000, 240_000)


@pytest.mark.asyncio
async def test_pinpoint_respects_the_publishers_visibility_flag() -> None:
    fetcher = FakeFetcher({"data": [_pinpoint_posting(compensation_visible=False)]})
    jobs = await Pinpoint().fetch_board(fetcher, "acme", with_details=False)
    assert jobs[0].salary_min is None
    assert jobs[0].salary_max is None
    assert jobs[0].salary_currency is None
    assert jobs[0].salary_raw is None


@pytest.mark.asyncio
async def test_pinpoint_without_a_frequency_settles_it_by_magnitude() -> None:
    """Dropping the figure would lose real data; the two bands don't overlap."""
    hourly = FakeFetcher({"data": [_pinpoint_posting(
        compensation_frequency=None, compensation=None)]})
    jobs = await Pinpoint().fetch_board(hourly, "acme", with_details=False)
    assert jobs[0].salary_min == 16 * 2080

    annual = FakeFetcher({"data": [_pinpoint_posting(
        compensation_frequency=None, compensation=None,
        compensation_minimum=120_000, compensation_maximum=160_000)]})
    jobs = await Pinpoint().fetch_board(annual, "acme", with_details=False)
    assert jobs[0].salary_min == 120_000
