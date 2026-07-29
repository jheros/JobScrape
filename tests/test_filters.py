"""Tests for the role/location/date classifiers.

These decide what ends up in the dataset, so they carry the most test weight.
"""

from __future__ import annotations

from datetime import date

import pytest

from jobscrape.filters import (
    apply_filters,
    in_window,
    is_software_role,
    is_us_eligible,
    parse_date,
    parse_us_location,
)
from jobscrape.models import Job


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "Staff Backend Engineer",
        "Full Stack Developer",
        "SDE II",
        "Site Reliability Engineer",
        "Machine Learning Engineer",
        "Data Engineer",
        "iOS Developer",
        "Security Engineer, Application Security",
        "Engineering Manager, Payments",
        "Principal Architect, Cloud Platform",
        "Python Developer",
        "QA Automation Engineer",
        "Embedded Firmware Engineer",
    ],
)
def test_software_titles_accepted(title: str) -> None:
    assert is_software_role(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "Mechanical Engineer",
        "Civil Engineer",
        "Process Engineer",
        "Sales Engineer",
        "Customer Success Engineer",
        "Audio Engineer",
        "Account Executive",
        "Technical Recruiter",
        "Product Manager",
        "Registered Nurse",
        "Warehouse Associate",
        "Business Developer",
    ],
)
def test_non_software_titles_rejected(title: str) -> None:
    assert is_software_role(title) is False


def test_bare_engineer_and_developer_qualify_alone() -> None:
    """Broadened deliberately so sparse titles are not missed."""
    assert is_software_role("Engineer", "Engineering") is True
    assert is_software_role("Engineer", None) is True
    assert is_software_role("Developer", None) is True
    assert is_software_role("Programmer", None) is True


def test_non_software_department_vetoes_a_bare_title() -> None:
    """A known-irrelevant department is evidence and should be used."""
    assert is_software_role("Engineer", "Facilities") is False
    assert is_software_role("Engineer", "Manufacturing") is False
    assert is_software_role("Developer", "Business Development") is False
    # An unknown department must never cause a rejection.
    assert is_software_role("Engineer", "Some New Team") is True


@pytest.mark.parametrize(
    "title",
    [
        "AI Engineer", "AI/ML Engineer", "Founding Engineer", "Production Engineer",
        "Product Engineer", "Web Engineer", "Implementations Engineer",
        "Growth Engineer", "Data Analyst", "Platform Developer",
        "Reliability Engineer", "Member of Technical Staff", "MLOps Engineer",
        "LLM Engineer", "Computer Vision Engineer", "BI Engineer",
        "Distributed Systems Engineer", "Kernel Developer", "Release Engineer",
        "Salesforce Developer", "ServiceNow Developer", "SAP ABAP Developer",
        "Siebel Developer", "Mainframe Engineer", "COBOL Programmer",
        "Rails Developer", "Flutter Developer", "Engineer, Platform",
        "New Grad Software Engineer", "Solidity Developer", "ETL Developer",
    ],
)
def test_expanded_titles_accepted(title: str) -> None:
    assert is_software_role(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "Water Engineer", "Traffic Engineer", "Survey Engineer",
        "Building Engineer", "Operating Engineer", "Wastewater Engineer",
        "Landscape Engineer", "Fire Engineer", "Recording Engineer",
        "Drilling Engineer", "Solar Developer", "Business Development Manager",
        "Technical Recruiter", "Engineering Recruiter", "Engineering Assistant",
        "Engine Mechanic",
    ],
)
def test_broadened_rules_still_reject_non_software(title: str) -> None:
    """The blocklist carries the weight now that bare titles match."""
    assert is_software_role(title) is False


def test_blocklist_beats_allowlist() -> None:
    # "Sales Engineer" contains "Engineer" but must never be treated as software.
    assert is_software_role("Sales Engineer", "Engineering") is False


@pytest.mark.parametrize(
    "location",
    [
        "Austin, TX",
        "San Francisco, CA",
        "New York, NY, United States",
        "Boston, Massachusetts",
        "Seattle",
        "Remote - US",
        "Remote (USA)",
        "United States",
    ],
)
def test_us_locations_accepted(location: str) -> None:
    assert is_us_eligible(location) is True


@pytest.mark.parametrize(
    "location",
    [
        "London",
        "Bangalore, India",
        "Toronto, Canada",
        "Berlin, Germany",
        "Israel, Raanana",
        "Remote - EMEA",
        "Singapore",
    ],
)
def test_named_non_us_locations_rejected(location: str) -> None:
    assert is_us_eligible(location) is False


@pytest.mark.parametrize(
    "location",
    [
        "Remote",                 # may well be open to the US
        "Worldwide",
        "Remote - Global",
        "Anywhere",
        "Somewhere Unlabeled",    # unrecognized
        "",                       # absent
    ],
)
def test_unknown_or_global_scope_accepted(location: str) -> None:
    """The filter is a can't-rule-it-out test: silence is not evidence."""
    assert is_us_eligible(location) is True


def test_missing_location_accepted() -> None:
    assert is_us_eligible(None) is True
    assert is_us_eligible("") is True


def test_bare_remote_accepted() -> None:
    """Inverted deliberately: an unqualified "Remote" can hire in the US."""
    assert is_us_eligible("Remote") is True
    assert is_us_eligible("Remote", is_remote=True) is True


@pytest.mark.parametrize(
    "location",
    [
        "Remote - EMEA",
        "Remote - APAC",
        "EU residents only",
        "Remote (India only)",
        "Europe only",
    ],
)
def test_remote_scoped_to_another_region_rejected(location: str) -> None:
    """Remote is not enough — it must be able to hire a US candidate."""
    assert is_us_eligible(location) is False


def test_us_signal_anywhere_wins() -> None:
    """Multi-location postings must survive a non-US mention."""
    assert is_us_eligible("New York / London") is True
    assert is_us_eligible("London or Remote (US)") is True


def test_structured_country() -> None:
    assert is_us_eligible("Somewhere", country="US") is True
    assert is_us_eligible("New York, NY", country="GB") is True  # US signal wins
    assert is_us_eligible("Munich", country="DE") is False


def test_parse_us_location() -> None:
    assert parse_us_location("Austin, TX") == ("Austin", "TX")
    assert parse_us_location("Boston, Massachusetts") == ("Boston", "MA")
    assert parse_us_location(None) == (None, None)


def _job(**kw: object) -> Job:
    base = dict(
        source="test", source_job_id="1", title="Software Engineer",
        company="Acme", job_url="https://example.com/1",
    )
    base.update(kw)
    return Job(**base)  # type: ignore[arg-type]


def test_date_window() -> None:
    job = _job(posted_at="2026-06-15")
    assert in_window(job, date(2026, 6, 1), date(2026, 6, 30)) is True
    assert in_window(job, date(2026, 7, 1), None) is False
    assert in_window(job, None, date(2026, 6, 1)) is False


def test_undated_jobs_excluded_when_window_requested() -> None:
    """An undated job cannot be shown to fall in the window, so it is dropped."""
    job = _job(posted_at=None)
    assert in_window(job, None, None) is True
    assert in_window(job, date(2026, 6, 1), None) is False


def test_parse_date_rejects_garbage() -> None:
    assert parse_date("2026-07-28") == date(2026, 7, 28)
    with pytest.raises(ValueError):
        parse_date("last tuesday")


def test_apply_filters_end_to_end() -> None:
    jobs = [
        _job(source_job_id="a", title="Software Engineer", location_raw="Austin, TX",
             posted_at="2026-06-10"),
        _job(source_job_id="b", title="Mechanical Engineer", location_raw="Detroit, MI",
             posted_at="2026-06-10"),
        _job(source_job_id="c", title="Software Engineer", location_raw="London",
             posted_at="2026-06-10"),
        _job(source_job_id="d", title="Software Engineer", location_raw="Austin, TX",
             posted_at="2025-01-01"),
    ]
    kept = apply_filters(jobs, since=date(2026, 6, 1), until=date(2026, 6, 30))
    assert [j.source_job_id for j in kept] == ["a"]
    assert kept[0].city == "Austin" and kept[0].state == "TX"
