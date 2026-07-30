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
    # Excluded by preference rather than by the blocklist. Present here so the
    # prefilter and the full filter are held together on them too — the
    # invariant below is one-directional, so a prefilter that forgot these
    # would not fail it, it would just download every QA posting and then
    # throw it away.
    "QA Engineer", "Test Engineer", "Automation Engineer", "SDET",
    "Engineering Manager", "Director of Engineering", "VP Engineering",
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


@pytest.mark.parametrize("title", [
    "QA Engineer", "Test Engineer", "Automation Engineer", "SDET",
    "Engineering Manager", "Director of Engineering", "VP Engineering",
])
def test_prefilter_excludes_the_same_roles_as_the_full_filter(title: str) -> None:
    """The one-directional invariant above cannot catch this case.

    The prefilter runs *before* the detail request, so an exclusion missing
    here costs one HTTP round-trip per posting and shows up only as an
    unexplained slowdown.
    """
    assert cheap_prefilter(title) is False
    assert is_software_role(title) is False
    # Both must relax together too.
    assert cheap_prefilter(title, exclude_qa=False, exclude_management=False) is True


def test_prefilter_accepts_bare_tech_titles_the_filter_accepts() -> None:
    """Regression: the prefilter did not consult `_BARE_TECH`.

    `is_software_role` accepts a title carrying only the bare word, so
    "VP Engineering" passed the filter while the prefilter skipped it — the
    posting was never fetched at all. Invisible until a title of that shape was
    tested, because the one-directional invariant above only fires when the full
    filter would keep it, and nothing in TITLES had that shape.
    """
    for title in ("VP Engineering", "Director of Engineering Operations",
                  "Engineering", "Dev"):
        assert cheap_prefilter(
            title, exclude_qa=False, exclude_management=False
        ) is True, f"prefilter skipped {title!r}, which the filter accepts"
