"""Tests for the role/location/date classifiers.

These decide what ends up in the dataset, so they carry the most test weight.
"""

from __future__ import annotations

from datetime import date

import pytest

from jobscrape.filters import (
    EXCLUDED_ROLE_NAMES,
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
        "Principal Architect, Cloud Platform",
        "Python Developer",
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


def test_the_bare_word_does_not_qualify_a_title() -> None:
    """The filter is an allowlist: a title must NAME a software discipline.

    "Engineer" alone used to be enough, with the blocklist expected to hold back
    every other profession that uses the word. It could not — measured against
    the archive that rule alone admitted 5,035 of 22,839 postings, four fifths of
    them hardware or generic. The word is shared; naming is what carries meaning.
    """
    assert is_software_role("Engineer", None) is False
    assert is_software_role("Developer", None) is False
    assert is_software_role("Senior Engineer", None) is False
    assert is_software_role("General Engineer", None) is False


def test_a_software_department_cannot_rescue_a_bare_title() -> None:
    """The department is not evidence that an engineer is a *software* engineer.

    This is the hole that made the first attempt at the inversion useless.
    `_GENERIC_TECH` contained the bare words and `_SOFTWARE_DEPTS` matches
    "Engineering" — which is the department nearly every engineering board files
    its postings under — so "Design Engineer" walked straight back in through the
    department fallback. It re-admitted 1,459 rows.
    """
    for title in ("Engineer", "Design Engineer", "Chief Engineer",
                  "Electronics Engineer", "Simulation Engineer"):
        assert is_software_role(title, "Engineering") is False, title
        assert is_software_role(title, "Software") is False, title


def test_genuinely_ambiguous_words_still_take_department_confirmation() -> None:
    """What the department fallback is actually for.

    "Scientist" and "Architect" are ambiguous rather than generic — a research
    biologist and a building architect are the competing readings, and neither
    appears in the hardware titles above. So the department still decides these.
    """
    assert is_software_role("Scientist", "Machine Learning") is True
    assert is_software_role("Scientist", None) is False
    assert is_software_role("Architect", "Platform Engineering") is True
    assert is_software_role("Architect", "Facilities") is False


@pytest.mark.parametrize(
    "title",
    [
        "AI Engineer", "AI/ML Engineer", "Founding Engineer", "Production Engineer",
        "Product Engineer", "Web Engineer", "Implementations Engineer",
        "Growth Engineer", "Platform Developer",
        "Reliability Engineer", "Member of Technical Staff", "MLOps Engineer",
        "LLM Engineer", "Computer Vision Engineer", "BI Engineer",
        "Distributed Systems Engineer", "Kernel Developer", "Release Engineer",
        "Mainframe Engineer", "COBOL Programmer",
        "Rails Developer", "Flutter Developer", "Engineer, Platform",
        "New Grad Software Engineer", "Solidity Developer", "ETL Developer",
        # Rescued when the bare word stopped qualifying: these were carried by
        # it alone and had to be named to survive.
        "UI Developer", "UI Engineer", "UI/UX Engineer", "Mobile App Developer",
        "Mobile Application Developer", "DevSecOps Engineer",
        "Applications Developer", "Agile Developer",
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
    """These are blocked by name, independently of the allowlist."""
    assert is_software_role(title) is False


@pytest.mark.parametrize(
    "title",
    [
        # Configuration and customisation of a vendor platform, not product
        # engineering. These used to be accepted as a named group.
        "Salesforce Developer", "Salesforce Engineer", "ServiceNow Developer",
        "SAP ABAP Developer", "Siebel Developer", "Workday Developer",
        "PeopleSoft Developer", "SharePoint Developer",
        # Pre-sales and consulting wearing the word "engineer".
        "Solutions Engineer", "Solutions Architect", "Senior Solutions Architect",
        "AI Solutions Engineer", "Cloud Solutions Architect",
        # Hardware and generic, which the bare word used to carry in.
        "Design Engineer", "Mechanical Design Engineer", "Simulation Engineer",
        "Device Engineer", "Design Verification Engineer", "FPGA Engineer",
        "Electronics Engineer", "Chief Engineer", "Field Service Engineer",
    ],
)
def test_titles_the_inversion_removes(title: str) -> None:
    """The roles this change exists to exclude, with a department to prove it.

    Passing a software department is the point — that is how the first attempt
    let them all back in.
    """
    assert is_software_role(title) is False
    assert is_software_role(title, "Engineering") is False


def test_forward_deployed_engineer_survives() -> None:
    """The one customer-adjacent title that stays: it writes code."""
    assert is_software_role("Forward Deployed Engineer") is True
    assert is_software_role("Senior Forward Deployed Engineer") is True


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


# --- QA and management: software roles we deliberately do not collect ------
#
# Distinct from the blocklist above, which means "not a software job at all".
# These ARE software jobs; they are excluded by preference and each preference
# is individually reversible.


#: QA titles the filter used to accept. These must round-trip: excluded by
#: default, re-admitted by the flag. That round-trip is what keeps the QA accept
#: patterns in `_SOFTWARE_TERMS` earning their place rather than being dead
#: weight to delete.
QA_PREVIOUSLY_ACCEPTED = [
    "QA Engineer", "Senior QA Engineer", "QA Automation Engineer",
    "Quality Assurance Engineer", "SDET",
    "Software Development Engineer in Test", "Software Engineer in Test",
    "Test Engineer", "Test Automation Engineer", "Automation Engineer",
]

#: QA titles that were *already* rejected before this change — "Quality
#: Engineer" is in the blocklist (`validation|quality|safety` + engineer), and
#: the rest match no accept pattern at all. `--include-qa` correctly does not
#: resurrect them; it re-admits what the filter used to keep, not everything
#: QA-shaped.
QA_ALREADY_REJECTED = [
    "Quality Engineer", "Software Tester", "Tester", "Test Analyst",
]


@pytest.mark.parametrize("title", QA_PREVIOUSLY_ACCEPTED)
def test_qa_roles_excluded(title: str) -> None:
    assert is_software_role(title) is False
    assert is_software_role(title, allow_roles=["qa"]) is True


@pytest.mark.parametrize("title", QA_ALREADY_REJECTED)
def test_qa_titles_the_filter_never_wanted_stay_rejected(title: str) -> None:
    assert is_software_role(title) is False
    assert is_software_role(title, allow_roles=["qa"]) is False


@pytest.mark.parametrize(
    "title",
    [
        "Engineering Manager", "Engineering Manager, Payments",
        "Senior Engineering Manager", "Manager, Software Engineering",
        "Director of Engineering", "Director of Engineering Operations",
        "Senior Director, Device Platform Engineering",
        "VP of Engineering", "VP Engineering", "Associate Director of Engineering",
    ],
)
def test_management_roles_excluded(title: str) -> None:
    assert is_software_role(title) is False
    assert is_software_role(title, allow_roles=["management"]) is True


@pytest.mark.parametrize(
    "title",
    [
        # Leads are individual contributors at most companies.
        "Tech Lead, Payments", "Technical Lead", "Engineering Lead",
        # And the ordinary senior IC ladder is untouched.
        "Staff Engineer", "Principal Engineer", "Senior Software Engineer",
        "Distinguished Engineer",
    ],
)
def test_senior_ic_roles_are_kept(title: str) -> None:
    assert is_software_role(title) is True


def test_chief_engineer_is_dropped_on_discipline_not_misread_as_management() -> None:
    """Two different reasons a title can be excluded, and only one applies here.

    "Chief Engineer" is a senior individual contributor in aerospace and
    defence, and the seniority module still says so — a naive `\\bchief\\b` rule
    would call it management and be wrong. It leaves the archive anyway, because
    the allowlist asks a different question: the title names no software
    discipline. That is the intended outcome for the ~69 postings carrying it,
    which were hardware.

    Worth pinning because the two layers could drift into agreeing for the wrong
    reason, and the seniority classification is used elsewhere.
    """
    from jobscrape import seniority
    from jobscrape.filters import MANAGEMENT_LEVELS, is_excluded_role

    for title in ("Chief Engineer", "Deputy Chief Engineer, Autonomous Flight"):
        assert seniority.classify(title) not in MANAGEMENT_LEVELS, title
        assert is_excluded_role(title) is None, title
        assert is_software_role(title, "Engineering") is False, title


def test_the_two_exclusions_are_independent() -> None:
    """Each flag re-admits only its own category."""
    assert is_software_role("QA Engineer", allow_roles=["management"]) is False
    assert is_software_role("Engineering Manager", allow_roles=["qa"]) is False


def test_exclusions_do_not_re_admit_non_software_roles() -> None:
    """--include-qa must not become a general "keep everything" switch."""
    for title in ("Mechanical Engineer", "Sales Engineer", "Registered Nurse"):
        assert is_software_role(
            title, allow_roles=EXCLUDED_ROLE_NAMES
        ) is False


# --- network / systems / security / analyst --------------------------------
#
# Four more categories held out by preference, same mechanism as QA and
# management. All four ARE software work under the accept lists; each is
# individually reversible with `--include-roles`.


@pytest.mark.parametrize(
    "title,category",
    [
        ("Network Engineer", "network"),
        ("Senior Network Engineer", "network"),
        ("Networking Engineer", "network"),

        ("Systems Engineer", "systems"),
        ("Senior Systems Engineer", "systems"),
        ("IT Systems Engineer", "systems"),
        ("Avionics Systems Engineer", "systems"),

        ("Systems Architect", "systems"),
        ("Security Engineer", "security"),
        ("Senior Security Engineer", "security"),
        ("Application Security Engineer", "security"),
        ("Product Security Engineer", "security"),
        ("Cyber Security Engineer", "security"),
        ("Cloud Security Engineer", "security"),

        ("Data Analyst", "analyst"),
        ("Senior Data Analyst", "analyst"),
        ("Analytics Engineer", "analyst"),
    ],
)
def test_excluded_categories(title: str, category: str) -> None:
    """Excluded by default, re-admitted by its own flag.

    Every title here was accepted by the filter before these categories
    existed, so the round-trip is meaningful — it is what keeps the
    corresponding accept patterns in `_SOFTWARE_TERMS` earning their place
    rather than being dead weight to delete.
    """
    from jobscrape.filters import cheap_prefilter, is_excluded_role

    assert is_excluded_role(title) == category
    assert is_software_role(title) is False
    # Both decision points must agree. The invariant test between them is
    # one-directional, so it cannot catch a prefilter that forgot a category.
    assert cheap_prefilter(title) is False
    assert is_software_role(title, allow_roles=[category]) is True


@pytest.mark.parametrize(
    "title,category",
    [
        ("Network Architect", "network"),
        ("Systems Administrator", "systems"),
        ("Security Analyst", "security"),
    ],
)
def test_titles_the_filter_never_wanted_stay_rejected(title: str, category: str) -> None:
    """These match a category but were never in the accept list either.

    `--include-roles` re-admits what the filter used to keep, not everything
    the category's pattern happens to match — there is no accept pattern for
    "Network Architect" or "Systems Administrator" to restore.
    """
    from jobscrape.filters import is_excluded_role

    assert is_excluded_role(title) == category
    assert is_software_role(title) is False
    assert is_software_role(title, allow_roles=[category]) is False


def test_distributed_systems_is_software_not_systems_engineering() -> None:
    """Backend stays. Distributed systems is core backend work and systems
    programming is OS/compiler work; neither is the systems-engineering
    discipline being excluded, and the suite asserted the first of these should
    be kept before the category existed."""
    from jobscrape.filters import is_excluded_role

    for title in ("Distributed Systems Engineer", "Senior Distributed Systems Engineer",
                  "Systems Programmer", "Senior Software Engineer - Distributed Systems"):
        assert is_excluded_role(title) is None, title
        assert is_software_role(title) is True, title


def test_embedded_systems_is_firmware_and_no_longer_protected() -> None:
    """Firmware goes. "Embedded Systems Engineer" was carved out of the systems
    rule as a software discipline; it is firmware work, and firmware is now its
    own excluded category."""
    from jobscrape.filters import is_excluded_role

    for title in ("Embedded Systems Engineer", "Embedded Software Engineer",
                  "Firmware Engineer", "Embedded Developer"):
        assert is_excluded_role(title) is not None, title
        assert is_software_role(title) is False, title


def test_each_flag_re_admits_only_its_own_category() -> None:
    from jobscrape.filters import EXCLUDED_ROLE_NAMES

    sample = {
        "qa": "QA Engineer", "management": "Engineering Manager",
        "network": "Network Engineer", "systems": "Systems Engineer",
        "security": "Security Engineer", "analyst": "Data Analyst",
        "firmware": "Firmware Engineer",
    }
    assert set(sample) == set(EXCLUDED_ROLE_NAMES), "a category has no test case"
    for category, title in sample.items():
        assert is_software_role(title, allow_roles=[category]) is True
        for other, other_title in sample.items():
            if other != category:
                assert is_software_role(other_title, allow_roles=[category]) is False


def test_the_analyst_rule_is_not_a_bare_analyst_match() -> None:
    """A bare `\banalyst\b` would take "Software Developer/Analyst" — a software
    role with an unfortunate title — and duplicate the security/systems rules."""
    from jobscrape.filters import is_excluded_role

    assert is_excluded_role("Software Developer/Analyst") is None
    assert is_excluded_role("Business Analyst") is None


@pytest.mark.parametrize(
    "title",
    ["Software Engineer", "Senior Backend Engineer", "Data Engineer",
     "Data Scientist", "Machine Learning Engineer", "Platform Engineer",
     "Site Reliability Engineer", "DevOps Engineer", "Infrastructure Engineer",
     "Cloud Engineer", "Full Stack Developer", "iOS Developer"],
)
def test_core_software_roles_survive_all_six_exclusions(title: str) -> None:
    """SRE, Platform, Infrastructure and Cloud are the ones a careless
    `systems` or `security` pattern would sweep up."""
    assert is_software_role(title) is True


# --- the category word need not sit next to the role noun ------------------
#
# The original patterns required adjacency, so "Systems Engineer" was caught
# while "Systems Integration Engineer" was not. Measured on 24,057 open
# postings, 1,481 titles named a category and NONE were caught — every adjacent
# form had already gone at fetch time, so what remained was entirely this shape.


@pytest.mark.parametrize(
    "title,category",
    [
        ("Systems Integration Engineer", "systems"),
        ("System Design Engineer", "systems"),
        ("System Verification Engineer", "systems"),
        ("Lead Systems Operations Engineer", "systems"),
        ("Sr. Systems Integration Engineer", "systems"),
        ("Network Operations Engineer", "network"),
        ("Network Infrastructure Engineer", "network"),
        ("Security Systems Design Engineer", "systems"),
        ("Security Operations Engineer", "security"),
    ],
)
def test_an_interposed_word_does_not_let_a_role_through(title: str, category: str) -> None:
    from jobscrape.filters import cheap_prefilter, is_excluded_role

    assert is_excluded_role(title) == category
    assert is_software_role(title) is False
    assert cheap_prefilter(title) is False


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer - Distributed Systems",
        "Staff Software Engineer, Security",
        "Machine Learning Engineer - Computer Vision & Data Systems",
        "Software Development Engineer - Security, Enterprise Technology Services",
        "Senior Backend Engineer, Payments Systems",
    ],
)
def test_a_software_head_noun_survives_a_trailing_category(title: str) -> None:
    """This is what separates the rule from a blanket keyword ban.

    The head noun is software and the category names the team or domain, so the
    posting is a software role that happens to mention systems or security.
    """
    from jobscrape.filters import is_excluded_role

    assert is_excluded_role(title) is None, title
    assert is_software_role(title) is True, title


def test_the_gap_is_bounded_at_two_words() -> None:
    """Unbounded, a category word at one end of a title would bind to a role
    noun at the other, which is how a targeted rule becomes a keyword ban."""
    from jobscrape.filters import is_excluded_role

    assert is_excluded_role("Systems Integration Engineer") == "systems"       # 1 word
    assert is_excluded_role("Systems Platform Integration Engineer") == "systems"  # 2
    # Four words between: too far to be the same noun phrase.
    assert is_excluded_role("Systems that support our growing team Engineer") is None
