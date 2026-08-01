"""Cornerstone and Paycom: the two bootstrap-then-search boards.

Both read a session token *and a request target* out of a page they do not
control, which makes them the only adapters where a hostile or compromised
career page could redirect an authenticated request. The allowlist tests below
are the ones that matter most.

Payloads mirror what these APIs actually returned when the adapters were
written, including the awkward parts: Cornerstone's US-locale ``M/D/YYYY``
dates, and Paycom's schema.org blob shipping an **empty** ``unitText`` with the
real period hiding in a prose field.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from jobscrape.sources.cornerstone import Cornerstone
from jobscrape.sources.paycom import Paycom


class FakeFetcher:
    """Serves queued responses by kind, recording every URL requested."""

    def __init__(self, texts: list[Any] | None = None,
                 posts: list[Any] | None = None,
                 gets: list[Any] | None = None) -> None:
        self._texts = list(texts or [])
        self._posts = list(posts or [])
        self._gets = list(gets or [])
        self.urls: list[str] = []

    async def get_text(self, url: str, **kw: Any) -> Any:
        self.urls.append(url)
        return self._texts.pop(0) if self._texts else None

    async def post_json(self, url: str, body: Any, **kw: Any) -> Any:
        self.urls.append(url)
        return self._posts.pop(0) if self._posts else None

    async def get_json(self, url: str, **kw: Any) -> Any:
        self.urls.append(url)
        return self._gets.pop(0) if self._gets else None


# =========================================================================
# Cornerstone
# =========================================================================

def _csod_page(host: str = "eu-fra.api.csod.com", token: str = "jwt-abc") -> str:
    return (
        "<html><script>csod.context.token = '%s';\n"
        'var api = "https://%s/rec-job-search";</script></html>' % (token, host)
    )


def _csod_reqs(n: int, start: int = 0) -> list[dict[str, Any]]:
    return [
        {
            "requisitionId": f"REQ{i}",
            "displayJobTitle": f"Engineer {i}",
            "locations": [{"city": "Tulsa", "state": "OK", "country": "United States"}],
            "externalDescription": "<p>Build things.</p>",
            "postingEffectiveDate": "7/29/2026",
            "schedule": "Full-time",
            "department": "Engineering",
        }
        for i in range(start, start + n)
    ]


def _csod_page_data(total: int, reqs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"data": {"totalCount": total, "requisitions": reqs}}


@pytest.mark.asyncio
async def test_cornerstone_parses_and_uses_the_regional_host_from_the_page() -> None:
    """The API host is per-tenant. Sampling 24 boards found us/uk/eu-fra/eu-cdg/au."""
    fetcher = FakeFetcher(
        texts=[_csod_page(host="uk.api.csod.com")],
        posts=[_csod_page_data(2, _csod_reqs(2))],
    )
    jobs = await Cornerstone().fetch_board(fetcher, "acme|1")
    assert jobs is not None and len(jobs) == 2
    assert any("uk.api.csod.com" in u for u in fetcher.urls), "must not assume a region"

    job = jobs[0]
    assert job.source_job_id == "acme:REQ0"
    assert job.title == "Engineer 0"
    assert job.location_raw == "Tulsa, OK, United States"
    assert job.country == "US"   # normalized from Cornerstone's "United States"
    assert job.department == "Engineering"
    # US-locale M/D/YYYY, not ISO.
    assert job.posted_at.startswith("2026-07-29")
    assert "Build things" in job.description_text
    assert "csod.com/ux/ats/careersite/1/job/REQ0" in job.job_url


@pytest.mark.asyncio
async def test_cornerstone_refuses_an_api_host_outside_csod() -> None:
    """The host comes from a page we don't control — it must be pinned."""
    page = (
        "<html><script>csod.context.token = 'jwt';\n"
        'var api = "https://evil.example.com/rec-job-search";</script></html>'
    )
    fetcher = FakeFetcher(texts=[page], posts=[_csod_page_data(1, _csod_reqs(1))])
    assert await Cornerstone().fetch_board(fetcher, "acme|1") is None
    # Only the career page was fetched; the token never left for the other host.
    assert len(fetcher.urls) == 1
    assert "evil.example.com" not in " ".join(fetcher.urls)


@pytest.mark.asyncio
async def test_cornerstone_paginates_to_the_reported_total() -> None:
    fetcher = FakeFetcher(
        texts=[_csod_page()],
        posts=[
            _csod_page_data(60, _csod_reqs(25, 0)),
            _csod_page_data(60, _csod_reqs(25, 25)),
            _csod_page_data(60, _csod_reqs(10, 50)),
        ],
    )
    jobs = await Cornerstone().fetch_board(fetcher, "acme|1")
    assert jobs is not None and len(jobs) == 60
    assert len({j.source_job_id for j in jobs}) == 60


@pytest.mark.asyncio
async def test_cornerstone_a_refused_search_is_a_failure_not_an_empty_board() -> None:
    """`[]` marks the board healthy; a board that always refuses is not."""
    fetcher = FakeFetcher(texts=[_csod_page()], posts=[None])
    assert await Cornerstone().fetch_board(fetcher, "acme|1") is None


@pytest.mark.asyncio
async def test_cornerstone_placeholder_descriptions_are_dropped() -> None:
    """"Please upload the job description" reads as real text to every check."""
    reqs = _csod_reqs(1)
    reqs[0]["externalDescription"] = "<p>Please upload the job description</p>"
    fetcher = FakeFetcher(texts=[_csod_page()], posts=[_csod_page_data(1, reqs)])
    jobs = await Cornerstone().fetch_board(fetcher, "acme|1")
    assert jobs[0].description_text is None
    assert jobs[0].description_html is None


@pytest.mark.asyncio
async def test_cornerstone_missing_board_returns_none() -> None:
    assert await Cornerstone().fetch_board(FakeFetcher(texts=[None]), "acme|1") is None
    # A page with no token is not a career site.
    assert await Cornerstone().fetch_board(
        FakeFetcher(texts=["<html>nothing here</html>"]), "acme|1") is None


@pytest.mark.parametrize(
    "slug,expected",
    [
        ("acme", ("acme", 1)),          # site defaults to 1
        ("acme|4", ("acme", 4)),        # a tenant's other career sites
        ("acme.evil.com|1", None),      # would escape the hostname label
        ("acme/../x|1", None),
        ("|1", None),
        ("acme|notanumber", None),
    ],
)
def test_cornerstone_slug_parsing(slug: str, expected: Any) -> None:
    from jobscrape.sources.cornerstone import parse_slug

    assert parse_slug(slug) == expected


@pytest.mark.asyncio
async def test_cornerstone_site_id_is_part_of_the_identity() -> None:
    """One tenant runs several career sites, each listing different jobs."""
    fetcher = FakeFetcher(texts=[_csod_page()], posts=[_csod_page_data(1, _csod_reqs(1))])
    jobs = await Cornerstone().fetch_board(fetcher, "acme|7")
    assert "/careersite/7/job/REQ0" in jobs[0].job_url


# =========================================================================
# Paycom
# =========================================================================

TOKEN = "0" * 31 + "a"


def _paycom_page(service: str = "https://portal-x.paycomonline.net") -> str:
    # A JWT whose payload decodes to {"clientcode": "0AB12"} — unsigned, since
    # nothing verifies it; it is read only to record the employer's code.
    jwt = "aaa.eyJjbGllbnRjb2RlIjogIjBBQjEyIn0.bbb"
    config = {"sessionJWT": jwt,
              "libConfig": json.dumps({"atsPortalMantleServiceUrl": service,
                                       "locale": "en-US"})}
    return f"<script>var configsFromHost = {json.dumps(config)};\nvar Mountable = 1;</script>"


def _previews(n: int) -> dict[str, Any]:
    return {
        "jobPostingPreviewsCount": n,
        "jobPostingPreviews": [
            {"jobId": 100 + i, "jobTitle": f"Engineer {i}",
             "locations": "Madison, WI 53713", "category": "Engineering",
             "positionType": "Full-time", "remoteType": "ONSITE"}
            for i in range(n)
        ],
    }


def _detail(salary_range: str | None, min_v: float, max_v: float,
            unit: str = "") -> dict[str, Any]:
    google = {
        "title": "Engineer 0",
        "datePosted": "2026-07-29",
        "employmentType": "FULL_TIME",
        "hiringOrganization": {"name": "Acme Clinic"},
        "jobLocation": {"address": {"addressCountry": "US"}},
        "baseSalary": {"currency": "USD",
                       "value": {"minValue": min_v, "maxValue": max_v, "unitText": unit}},
    }
    detail: dict[str, Any] = {
        "jobTitle": "Engineer 0",
        "jobDescription": "<p>Do the work.</p>",
        "googleJobJson": json.dumps(google),
    }
    if salary_range is not None:
        detail["salaryRange"] = salary_range
    return {"jobPosting": detail}


@pytest.mark.asyncio
async def test_paycom_hourly_pay_is_annualised_from_the_prose_field() -> None:
    """`unitText` was empty on all 74 postings measured; the period is in
    `salaryRange`. Without it, 22 lands in the column that holds 220000."""
    fetcher = FakeFetcher(
        texts=[_paycom_page()],
        posts=[_previews(1)],
        gets=[_detail("$22.00 - $24.00 Hourly", 22, 24)],
    )
    jobs = await Paycom().fetch_board(fetcher, TOKEN)
    assert jobs is not None and len(jobs) == 1
    job = jobs[0]
    assert job.salary_min == 22 * 2080 and job.salary_max == 24 * 2080
    assert job.salary_currency == "USD"
    # The published text stays on record so the conversion is auditable.
    assert job.salary_raw == "$22.00 - $24.00 Hourly"
    assert job.company == "Acme Clinic"
    assert job.country == "US"
    assert job.employment_type == "full_time"   # normalized from Paycom's "Full-time"
    assert job.posted_at.startswith("2026-07-29")


@pytest.mark.asyncio
async def test_paycom_annual_pay_is_left_alone() -> None:
    fetcher = FakeFetcher(
        texts=[_paycom_page()], posts=[_previews(1)],
        gets=[_detail("$60,000.00 - $175,000.00 Salary/year", 60000, 175000)],
    )
    jobs = await Paycom().fetch_board(fetcher, TOKEN)
    assert (jobs[0].salary_min, jobs[0].salary_max) == (60000, 175000)


@pytest.mark.asyncio
async def test_paycom_an_unnamed_period_is_settled_by_magnitude() -> None:
    """Some tenants write just "Salary". The two populations don't overlap."""
    annual = FakeFetcher(texts=[_paycom_page()], posts=[_previews(1)],
                         gets=[_detail("$60,000.00 - $175,000.00 Salary", 60000, 175000)])
    assert (await Paycom().fetch_board(annual, TOKEN))[0].salary_min == 60000

    hourly = FakeFetcher(texts=[_paycom_page()], posts=[_previews(1)],
                         gets=[_detail("$20.00 - $21.00", 20, 21)])
    assert (await Paycom().fetch_board(hourly, TOKEN))[0].salary_min == 20 * 2080


@pytest.mark.asyncio
async def test_paycom_an_explicit_unittext_wins_over_the_prose() -> None:
    fetcher = FakeFetcher(texts=[_paycom_page()], posts=[_previews(1)],
                          gets=[_detail("$50.00 - $60.00 Hourly", 50, 60, unit="YEAR")])
    assert (await Paycom().fetch_board(fetcher, TOKEN))[0].salary_min == 50


@pytest.mark.asyncio
async def test_paycom_refuses_a_service_host_outside_paycom() -> None:
    """`libConfig` names where the session JWT gets sent. It must be pinned."""
    fetcher = FakeFetcher(texts=[_paycom_page(service="https://evil.example.com")],
                          posts=[_previews(1)])
    assert await Paycom().fetch_board(fetcher, TOKEN) is None
    assert "evil.example.com" not in " ".join(fetcher.urls)


@pytest.mark.asyncio
async def test_paycom_refuses_a_lookalike_host() -> None:
    """`paycomonline.net.evil.com` must not pass a naive suffix check."""
    fetcher = FakeFetcher(
        texts=[_paycom_page(service="https://portal.paycomonline.net.evil.com")],
        posts=[_previews(1)],
    )
    assert await Paycom().fetch_board(fetcher, TOKEN) is None


@pytest.mark.asyncio
async def test_paycom_malformed_google_metadata_does_not_lose_the_posting() -> None:
    """Structured metadata is an enrichment; the job is still usable without it."""
    broken = _detail(None, 0, 0)
    broken["jobPosting"]["googleJobJson"] = "{not json"
    fetcher = FakeFetcher(texts=[_paycom_page()], posts=[_previews(1)], gets=[broken])
    jobs = await Paycom().fetch_board(fetcher, TOKEN)
    assert len(jobs) == 1
    assert "Do the work" in jobs[0].description_text


@pytest.mark.asyncio
async def test_paycom_a_refused_search_is_a_failure_not_an_empty_board() -> None:
    fetcher = FakeFetcher(texts=[_paycom_page()], posts=[None])
    assert await Paycom().fetch_board(fetcher, TOKEN) is None


@pytest.mark.asyncio
async def test_paycom_duplicate_job_ids_collapse() -> None:
    """A posting listed once per location must not become several rows."""
    payload = _previews(1)
    payload["jobPostingPreviews"].append(dict(payload["jobPostingPreviews"][0]))
    payload["jobPostingPreviewsCount"] = 2
    fetcher = FakeFetcher(texts=[_paycom_page()], posts=[payload],
                          gets=[_detail("$20.00 Hourly", 20, 20)])
    jobs = await Paycom().fetch_board(fetcher, TOKEN)
    assert len(jobs) == 1


@pytest.mark.parametrize(
    "slug,ok",
    [
        ("0" * 32, True),
        ("0" * 31 + "A", True),                                    # case-insensitive
        (f"https://www.paycomonline.net/v4/ats/web.php/portal/{'a'*32}/career-page", True),
        ("0" * 31 + "g", False),                                   # not hex
        ("0" * 31, False),                                         # too short
        ("", False),
    ],
)
def test_paycom_slug_parsing(slug: str, ok: bool) -> None:
    from jobscrape.sources.paycom import parse_slug

    assert (parse_slug(slug) is not None) is ok


@pytest.mark.asyncio
async def test_paycom_missing_portal_returns_none() -> None:
    # Paycom answers 200 with a plain sentence for a token that isn't a board.
    fetcher = FakeFetcher(texts=["<div>Job board does not exist or is unavailable.</div>"])
    assert await Paycom().fetch_board(fetcher, TOKEN) is None


@pytest.mark.asyncio
async def test_both_probes_cost_one_page_and_one_search() -> None:
    """Probing runs across thousands of slugs; it must not fan out to details."""
    csod = FakeFetcher(texts=[_csod_page()], posts=[_csod_page_data(200, _csod_reqs(25))])
    assert await Cornerstone().probe(csod, "acme|1") == ("live", 25)
    assert len(csod.urls) == 2

    pay = FakeFetcher(texts=[_paycom_page()], posts=[_previews(5)])
    assert await Paycom().probe(pay, TOKEN) == ("live", 5)
    assert len(pay.urls) == 2, "no detail requests while probing"
