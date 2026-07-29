"""The prefilter must never discard a job the real filters would keep.

Ashby, SmartRecruiters and Workday need a second request per posting, so the
prefilter decides — from listing data alone — whether that request is worth
making. A false negative here silently loses a real job and no later stage can
recover it, which makes this invariant the important one to protect.
"""

from __future__ import annotations

import itertools

import pytest

from jobscrape.filters import cheap_prefilter, is_software_role, is_us_location

TITLES = [
    "Senior Software Engineer", "Staff Backend Engineer", "SDE II",
    "Machine Learning Engineer", "iOS Developer", "Full Stack Developer",
    "Principal Architect, Cloud Platform", "Data Scientist", "Engineer",
    "Site Reliability Engineer", "Security Engineer",
    "Mechanical Engineer", "Sales Engineer", "Account Executive",
    "Recruiter", "Audio Engineer", "Product Manager",
]
LOCATIONS = [
    None, "", "Austin, TX", "San Francisco, CA", "Remote - US", "United States",
    "New York, NY", "London", "Bangalore, India", "Berlin, Germany", "Remote",
]
DEPARTMENTS = [None, "Engineering", "Sales", "Facilities"]


@pytest.mark.parametrize(
    "title,location,department",
    list(itertools.product(TITLES, LOCATIONS, DEPARTMENTS)),
)
def test_prefilter_never_rejects_a_keeper(
    title: str, location: str | None, department: str | None
) -> None:
    would_keep = is_software_role(title, department) and is_us_location(location)
    if would_keep:
        assert cheap_prefilter(title, department, location) is True, (
            f"prefilter dropped a job the filters would keep: "
            f"{title!r} / {department!r} / {location!r}"
        )


def test_prefilter_rejects_obvious_waste() -> None:
    assert cheap_prefilter("Sales Engineer", None, "Austin, TX") is False
    assert cheap_prefilter("Mechanical Engineer", None, "Detroit, MI") is False
    assert cheap_prefilter("Account Executive", None, "NYC") is False
    assert cheap_prefilter("Backend Engineer", None, "Berlin, Germany") is False


def test_prefilter_is_permissive_when_uncertain() -> None:
    """Missing or ambiguous data must pass through, not be guessed away."""
    assert cheap_prefilter(None) is True
    assert cheap_prefilter("") is True
    assert cheap_prefilter("Software Engineer", None, None) is True
    assert cheap_prefilter("Engineer", None, None) is True
    # US-scoped remote must survive even though "Remote" alone would not.
    assert cheap_prefilter("Software Engineer", None, "Remote - US") is True
