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
    # "Architect" is now an excluded PREFERENCE, checked before the department
    # fallback ever runs — so the department can no longer confirm it, and the
    # flag is what restores it instead. The fallback itself is unchanged; this
    # title simply stopped being one of its cases.
    assert is_software_role("Architect", "Platform Engineering") is False
    assert is_software_role("Architect", "Platform Engineering",
                            allow_roles={"architect"}) is True
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
        "Rails Developer", "Flutter Developer", "Engineer, Platform",
        "Solidity Developer", "ETL Developer",
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


@pytest.mark.parametrize(
    "title",
    [
        "iOS Frameworks Engineer - Services Special Project",
        "iOS SpringBoard Engineer - Graphic Wallpapers",
        "Machine Learning Video Processing Engineer",
        "Machine Learning Ops Engineer",
        "Senior Software Advanced Developer",
        "Software Integrity Engineer, Sensing & Connectivity",
        "Senior API / Microservices Developer",
        "Cloud Operations Engineer",
        "AI Agent Engineer",
        "Artificial Intelligence (AI) Engineer",
        "Senior Inference Engineer, GPU Kernel Optimization",
    ],
)
def test_a_word_between_the_qualifier_and_the_noun_does_not_lose_the_job(
    title: str,
) -> None:
    """The failure mode the allowlist introduces, and the fix for it.

    Nearly every accept pattern requires its qualifier to touch the role noun.
    That was survivable while a bare "Engineer" qualified on its own — the broad
    rule caught what the specific ones missed. As an allowlist it is not: the
    first purge dry-run had all of these queued for deletion because a word sat
    in the middle. Same adjacency bug the exclusion categories had, same bounded
    gap fixes it.
    """
    assert is_software_role(title) is True


@pytest.mark.parametrize(
    "title",
    [
        # The gap must not reach across a title to bind a stray word.
        "Design Engineer", "Mechanical Design Engineer", "Data Center Engineer",
        "Hardware Design Engineer", "Design Verification Engineer",
        "Electronics Engineer", "Field Service Engineer", "Simulation Engineer",
    ],
)
def test_the_gap_does_not_re_admit_hardware(title: str) -> None:
    """"design", "systems" and "data" are kept OUT of the qualifier list.

    Those are exactly the words where the middle of a title decides the
    discipline — with a gap, "data" alone matches "Data Center Engineer", which
    is facilities.
    """
    assert is_software_role(title) is False
    assert is_software_role(title, "Engineering") is False


def test_forward_deployed_engineer_survives() -> None:
    """The one customer-adjacent title that stays: it writes code."""
    assert is_software_role("Forward Deployed Engineer") is True
    assert is_software_role("Senior Forward Deployed Engineer") is True


def test_pre_sales_outranks_the_forward_deployed_accept() -> None:
    """When both rules apply, the blocklist wins — and should.

    "Forward Deployed Engineer | Pre-sales" names the accept pattern and the
    blocked one. The work is pre-sales, so the block is the right answer; this
    pins the precedence rather than leaving it to list order.
    """
    assert is_software_role("Forward Deployed Engineer | Pre-sales") is False


# --- the blocklist's adjacency, fixed to match the accept list's -----------


@pytest.mark.parametrize(
    "title",
    [
        "Mechanical Reliability Engineer",
        "Hardware Integration Engineer",
        "Senior Hardware Reliability Engineer",
        "Senior VLSI Integration Engineer",
        "Senior RTL Integration Engineer",
        "SoC Silicon Reliability Engineer",
        "IC Packaging Integration Engineer",
        "Senior Electrical Product Engineer - Datacenter",
        "Optical Design Engineer",
        "Senior Silicon Reliability Engineer",
    ],
)
def test_a_hardware_discipline_reaches_the_role_noun(title: str) -> None:
    """`_NOT_SOFTWARE_TERMS` used to require its discipline word to touch.

    It matched "Mechanical Engineer" and missed "Mechanical Reliability
    Engineer". Harmless while a bare "Engineer" qualified on its own; once the
    allowlist began accepting generic role nouns — `integration engineer`,
    `reliability engineer`, `performance engineer` — that gap became the way
    hardware got in. 85 postings.
    """
    assert is_software_role(title) is False
    assert is_software_role(title, "Engineering") is False


@pytest.mark.parametrize(
    "title",
    [
        "Optical Software Engineer",
        "Senior VLSI CAD Software Engineer",
        "Silicon Validation Software Engineer: CPU and Memory Hierarchy",
        "Analog Mixed-Signal Modeling Software Engineer",
        "VLSI Design Automation Software Engineer",
        "Software Development Engineer - Silicon Development Infrastructure",
    ],
)
def test_software_engineers_on_hardware_teams_survive(title: str) -> None:
    """The exemption that makes the rule above safe rather than harmful.

    Eight of those 85 say "Software Engineer" outright. They write software for
    hardware teams, and they are the difference between the rule removing 77
    wrong rows and removing 85 including 8 right ones. Only the explicit
    spelling counts — "Optical Design Engineer" names no software role.
    """
    assert is_software_role(title) is True


@pytest.mark.parametrize(
    "title",
    [
        # A discipline an intervening word genuinely can make software. These
        # stay adjacency-only, which is why the gap uses a curated subset.
        "Validation Platform Engineer",
        "Field Reliability Software Engineer",
        "Energy Data Platform Engineer",
        "Transportation Data Engineer",
        "Manufacturing Data Platform Engineer",
    ],
)
def test_the_gap_uses_a_curated_subset_not_the_whole_blocklist(title: str) -> None:
    """`_NOT_SOFTWARE_TERMS` names validation, field, energy, transportation and
    manufacturing too. Those keep requiring adjacency, because an intervening
    word genuinely can make them software — which is why the gap runs over a
    curated list of physical disciplines rather than the whole blocklist."""
    assert is_software_role(title) is True


# --- roles that route around a removed accept ------------------------------


@pytest.mark.parametrize(
    "title",
    [
        # Strategy and governance, same family as Solutions Architect.
        "Enterprise Architect", "Senior Enterprise Architect", "Business Architect",
        "Enterprise Architect Java", "IT Enterprise Architect",
        # The vendor accept was removed last round; these came back in through
        # `technical architect` and `tech lead`, which ask for no qualifier.
        "Salesforce Technical Architect", "Salesforce Technical Lead",
        "ServiceNow Senior Tech Lead", "NetSuite Architect",
        "SAP S/4HANA Technical Lead - ABAP & Fiori",
        "Senior Technical Consultant - Salesforce DevOps",
        # Pre-sales in a role noun other than "engineer".
        "Technical Architect (Pre-Sales)", "Pre-Sales Data and AI Architect",
        "Partner Technical Architect - Agentforce",
        # Churned-listing tell: seniority words run together.
        "SeniorStaffPrincipal Software Consultant FullStack Morning Shift",
        "PrincipalStaff Test Consultant Playwright Azure DevOps",
    ],
)
def test_titles_that_routed_around_a_removed_accept(title: str) -> None:
    assert is_software_role(title) is False
    assert is_software_role(title, "Engineering") is False


def test_genuine_consultancies_and_shift_work_survive() -> None:
    """No shift-word rule was built, and this is what would break if one were.

    A shift in the title matched 16 postings but only 2 of the body-shop
    listings it was aimed at; three were real SRE and DevOps rotations. The
    mashed-seniority half of the rule ships; the shift half does not.
    """
    for title in (
        "Cloud Operations Engineer (2nd Shift, Weekend)",
        "Site Reliability Engineer (Tu-Sat, night shift)",
        "Quantitative Technologist (Full-Time - DevOps - Night Shift)",
        "Senior Consultant, Full Stack Engineer (Node.js/React/Typescript/AWS)",
        "Senior Consultant, Mobile App Engineer",
    ):
        assert is_software_role(title) is True, title


# --- the bare `programmer` accept, qualified -------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "CNC Programmer", "CNC Programmer II, Second Shift", "Lead CMM Programmer",
        "NC Programmer - Senior", "CNC / NC Programmer",
        "Instrumentation and Controls Programmer",
        "Engineering Tech - Laser and Tool Room Programmer 1st Shift",
        "Lead Metering Programmer",
        "Metrology Engineer/CMM Programmer, Maritime Division",
        "Programmer Analyst II", "Senior Programmer Analyst-Oracle",
    ],
)
def test_machinist_and_clerical_programmers_are_not_software(title: str) -> None:
    """28 machinists were sitting in a software archive.

    Found by probing a rule that turned out to be wrong for its own purpose —
    a shift-word filter matched them, which is how they surfaced at all.
    """
    assert is_software_role(title) is False


@pytest.mark.parametrize(
    "title",
    [
        "Engine Programmer", "Sound Programmer", "C++ Programmer",
        "Senior UI Programmer", "Senior Language Programmer",
        "Research Programmer II", "Senior Scientific Programmer",
        "High Performance Low Level Programmer",
    ],
)
def test_programmer_stays_a_software_word(title: str) -> None:
    """Games and research use "Programmer" as the ordinary word for the job.

    Which is why the machining senses are blocked by name rather than the accept
    being removed.
    """
    assert is_software_role(title) is True


# --- early career and flight: two more preference categories ---------------


@pytest.mark.parametrize(
    "title,category",
    [
        ("Software Engineering Intern", "early_career"),
        ("Software Engineer Intern", "early_career"),
        ("Software Developer Intern", "early_career"),
        ("Software Engineer (New Grad)", "early_career"),
        ("New Grad Software Engineer", "early_career"),
        ("Data Science Intern", "early_career"),
        ("Software Engineering Co-op", "early_career"),
        ("Senior Flight Software Engineer", "flight"),
        ("Flight Software Engineer II", "flight"),
        ("Space Flight Software Engineer, C++", "flight"),
        ("Avionics Integration Engineer", "flight"),
        ("Avionics Production Engineer", "flight"),
    ],
)
def test_the_new_categories_round_trip(title: str, category: str) -> None:
    """Excluded by default, restored by their own flag.

    The round-trip is the point: an exclusion whose accept pattern does not name
    the same titles gives a flag that silently half-works. "Data Science Intern"
    is here because it caught exactly that — the accept list spelled
    "data scientist" but not "data science".
    """
    from jobscrape.filters import is_excluded_role

    assert is_excluded_role(title) == category
    assert is_software_role(title) is False
    assert is_software_role(title, allow_roles=[category]) is True


@pytest.mark.parametrize(
    "title",
    [
        "Internal Tools Engineer", "Senior Software Engineer, Internal Tools",
        "Software Engineer, Internal Platforms",
        "Internationalization Software Engineer, Input Experience",
        "International Payments Software Engineer",
    ],
)
def test_early_career_does_not_catch_internal_or_international(title: str) -> None:
    """The trap in this rule, and the reason it is word-anchored.

    A bare `intern` substring matches "internal" and "internationalization"; 13
    titles in the archive carry one or the other.
    """
    from jobscrape.filters import is_excluded_role

    assert is_excluded_role(title) != "early_career"


@pytest.mark.parametrize(
    "title",
    [
        # Chip design reaching a software word two positions away.
        "Senior AI Chip Design Engineer", "SoC Machine Learning Design Engineer",
        "AI CPU Design Engineer", "Senior AI Design Engineer",
        "SoC Physical Design Engineer", "Senior Digital Design Engineer",
        "2026 Campus - SoC Backend Design Engineer",
        # Device management and modelling.
        "Mobile Device Engineer", "Mobile Device Engineer II",
        "MTS Lead DRAM Device Engineer",
        "Modeling and Simulation Engineer - Data Analytics",
        "Senior Machine Learning and Simulation Engineer - Autonomous Vehicles",
        # A slash is not enough to make a chip role software.
        "Hardware/Software Co-Design Engineer",
        "Senior Hardware/Software Design Engineer (HSDE-3)",
    ],
)
def test_design_simulation_and_device_are_blocked_wherever_they_appear(
    title: str,
) -> None:
    """The accept side's own gap was binding these to a software word.

    "Senior AI Chip Design Engineer" qualified on `ai`, "SoC Machine Learning
    Design Engineer" on `machine learning`, "Mobile Device Engineer" on
    `mobile` — all two positions away, all chip design or device management.
    Leaving the three nouns out of the accept list is not enough; they have to
    be blocked.
    """
    assert is_software_role(title) is False
    assert is_software_role(title, "Engineering") is False


@pytest.mark.parametrize(
    "title",
    [
        "SAI Software Design Engineer",
        "Software Product Design Engineer",
        "Senior Software Simulation Engineer",
        "Senior Software Design Engineer - Engine Controls",
    ],
)
def test_the_software_word_rescues_a_design_title(title: str) -> None:
    """`software` with no `hardware` is the exemption, not just "Software Engineer".

    None of these has the qualifier touching the noun, so the strict spelling
    alone would drop all four.
    """
    assert is_software_role(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "Salesforce Platform Engineer", "Salesforce DevOps Engineer",
        "Salesforce Full Stack Engineer", "Salesforce Integration Developer-CRM",
        "Salesforce Marketing Cloud Developer", "Senior Salesforce Software Engineer",
        "Software Engineer - Salesforce Platform", "Sr. Software Engineer- Salesforce",
        "Software Engineering MTS, Salesforce Engineer",
        "Systems Engineering Associate - GovCloud [Salesforce National Security]",
    ],
)
def test_salesforce_engineers_are_blocked_from_either_side(title: str) -> None:
    """You named Salesforce; the vendor accept going was not enough.

    They came back through `platform engineer`, `devops`, `full stack` and
    `software engineer`. The word sits on either side of the role noun, so this
    tests both orders — and `engineering` as well as `engineer`, which one
    posting used.
    """
    assert is_software_role(title) is False


def test_salesforce_as_an_employer_is_not_salesforce_as_a_platform() -> None:
    """The ambiguity in the rule above, and where the line falls.

    "Research Scientist - Salesforce AI Research" is AI research *at* Salesforce
    — no engineer or developer in the title, so the two-lookahead form leaves it
    alone. That is the intended behaviour, not an oversight.
    """
    assert is_software_role("Research Scientist - Salesforce AI Research") is True


@pytest.mark.parametrize(
    "title",
    [
        "Devops/ Java/OBIEE/Salesforce  training and placement",
        "ETL Informatica/ Networking/ Salesforce/Devops training and placemt",
    ],
)
def test_bootcamp_adverts_are_not_job_postings(title: str) -> None:
    """`plac\\w+` rather than a spelled-out "placement": these listings are
    typo-ridden, and one in the archive reads "placemt"."""
    assert is_software_role(title) is False


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer - 2027 New Grads",
        "Member of Technical Staff - Machine Learning Capabilities, New Graduates",
        "New College Graduates Welcome",
    ],
)
def test_early_career_catches_plurals(title: str) -> None:
    """Three postings escaped the first version of the rule on a trailing "s"."""
    from jobscrape.filters import is_excluded_role

    assert is_excluded_role(title) == "early_career"


def test_flight_engineer_is_a_different_job_from_flight_software() -> None:
    """Both excluded, for different reasons that should not be conflated.

    "Flight Engineer" is cockpit crew and has always been in the blocklist —
    not a software job at all. "Flight Software Engineer" is genuine C++ work,
    excluded by preference as embedded, and restorable with a flag. Only the
    second belongs to the category.
    """
    from jobscrape.filters import is_excluded_role

    assert is_excluded_role("Flight Engineer") is None
    assert is_software_role("Flight Engineer", allow_roles=["flight"]) is False
    assert is_excluded_role("Flight Software Engineer") == "flight"
    assert is_software_role("Flight Software Engineer", allow_roles=["flight"]) is True


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


@pytest.mark.parametrize(
    "location,country",
    [
        ("Barcelona, CT, Spain", "ES"),   # CT = Cataluña, not Connecticut
        ("Madrid, MD, Spain", "ES"),      # MD = Madrid region, not Maryland
        ("Zaragoza, AR, Spain", "ES"),    # AR = Aragón, not Arkansas
        ("Georgia", "GE"),                # the country, not the US state
        ("Tbilisi, Georgia", "GE"),
    ],
)
def test_a_foreign_region_code_is_not_read_as_a_us_state(location: str, country: str) -> None:
    """Several US state abbreviations collide exactly with a foreign
    region's own 2-letter code, and "Georgia" collides with a US state by
    full name too — a resolved, non-US country is stronger evidence than
    either text pattern and must win. Real data: dozens of Spain-only
    SmartRecruiters postings and Georgia(country)-only Breezy postings were
    kept as "could be US-eligible" this way."""
    assert is_us_eligible(location, country=country) is False


def test_a_us_state_still_wins_with_no_country_given() -> None:
    """The collision guard only fires when a country is actually resolved
    and non-US — an untagged posting still gets the benefit of the doubt."""
    assert is_us_eligible("Barcelona, CT, Spain") is True
    assert is_us_eligible("Georgia") is True


def test_parse_us_location() -> None:
    assert parse_us_location("Austin, TX") == ("Austin", "TX")
    assert parse_us_location("Boston, Massachusetts") == ("Boston", "MA")
    assert parse_us_location(None) == (None, None)


def test_parse_us_location_rejects_a_country_prefix_as_the_city() -> None:
    """Workday orders some postings "country, state, city" instead of the
    usual "city, state" — cutting text at the state name left the leading
    country code ("US"/"USA") stored as `city`, and the real city (after the
    state) was never even looked at. A leftover that isn't a real place
    should come back `None`, not a country code."""
    assert parse_us_location("US, Minnesota, Eagan") == (None, "MN")
    assert parse_us_location("USA Minnesota Eden Prairie") == (None, "MN")


def test_parse_us_location_rejects_a_workplace_word_as_the_city() -> None:
    """"Remote - Connecticut" left `city = "Remote -"` (the noise-stripping
    regex only stripped the trailing comma, not the dash) — a real state
    should still resolve, but the leftover noise must not become the city."""
    assert parse_us_location("Remote - Connecticut") == (None, "CT")


def test_parse_us_location_rejects_a_requisition_code_as_the_city() -> None:
    """"09507 - Colorado Remote" left `city = "09507"` — an internal
    requisition code, not a city; `_CITY_NOISE_RE` only strips specific
    words, not arbitrary numbers, so this needs its own guard."""
    assert parse_us_location("09507 - Colorado Remote") == (None, "CO")


def test_parse_us_location_rejects_a_not_a_place_sentinel_as_the_city() -> None:
    """"TBD, NY" left `city = "TBD"` -- `_CITY_NOISE_RE` only strips
    workplace-word decoration (remote/hybrid/onsite/us/usa/...), not the
    rest of `models._NOT_A_PLACE` ("TBD", "N/A", "unknown", ...), and "TBD"
    passes the has-letters check that catches a bare requisition code. A
    real state should still resolve; the sentinel must not become the city."""
    assert parse_us_location("TBD, NY") == (None, "NY")
    assert parse_us_location("N/A, California") == (None, "CA")


def test_parse_us_location_does_not_store_a_foreign_region_code_as_a_state() -> None:
    """Same collision as `is_us_eligible`'s guard, but here the consequence
    is a wrong *value* rather than a wrongly-kept row: "Barcelona, CT,
    Spain" used to store `state="CT"` as if Barcelona were in Connecticut."""
    assert parse_us_location("Barcelona, CT, Spain", "ES") == (None, None)
    assert parse_us_location("Madrid, MD, Spain", "ES") == (None, None)
    assert parse_us_location("Georgia", "GE") == (None, None)
    # No country given: benefit of the doubt, same as before this fix.
    assert parse_us_location("Barcelona, CT, Spain") == ("Barcelona", "CT")


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
    # remote_only=False: this test is about role/location/date filtering, not
    # workplace_type -- none of these postings say anything about remote work.
    kept = apply_filters(
        jobs, since=date(2026, 6, 1), until=date(2026, 6, 30), remote_only=False,
    )
    assert [j.source_job_id for j in kept] == ["a"]
    assert kept[0].city == "Austin" and kept[0].state == "TX"


def test_an_adapter_supplied_foreign_state_is_cleared_not_just_parsed_ones() -> None:
    """SmartRecruiters sets `city`/`state` itself from its own structured
    location (Spain's "CT" province code for Cataluña) rather than leaving
    them blank for `parse_us_location` to fill — so the fallback above never
    even runs for this row, and only an unconditional check catches it."""
    job = _job(
        source_job_id="es-1", title="Software Engineer",
        location_raw="Barcelona, CT, Spain", city="Barcelona", state="CT",
        country="ES", posted_at="2026-06-10",
    )
    # remote_only=False: this test is about US-eligibility/location handling,
    # not workplace_type.
    kept = apply_filters(
        [job], since=date(2026, 6, 1), until=date(2026, 6, 30), remote_only=False,
    )
    assert kept == [], "Spain-only posting must not pass US eligibility"

    # A row that IS kept for a real reason (a genuine US mention elsewhere)
    # still has its state cleared, since "CT" doesn't describe a US office.
    mixed = _job(
        source_job_id="es-2", title="Software Engineer",
        location_raw="Barcelona, CT, Spain or New York, US",
        city="Barcelona", state="CT", country="ES", posted_at="2026-06-10",
    )
    kept = apply_filters(
        [mixed], since=date(2026, 6, 1), until=date(2026, 6, 30), remote_only=False,
    )
    assert len(kept) == 1
    assert kept[0].state is None


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
        # "Avionics Systems Engineer" used to live here. It is now held by
        # `flight` as well, so `--include-roles systems` alone cannot restore it
        # — correctly, since it is an avionics role. Two categories overlapping
        # is fine; asserting a single-flag round-trip on such a title is not.
        ("Spacecraft Data Systems Engineer", "systems"),
        # "Systems Architect" left for the same reason, when `architect` became
        # a category — see test_two_categories_both_have_to_be_released.

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


def test_two_categories_both_have_to_be_released() -> None:
    """A title can sit in more than one excluded category, and then one flag is
    not enough — which is correct, not a bug.

    "Systems Architect" is the case `architect` created: it was a `systems`
    round-trip case until that category existed. `is_excluded_role` reports the
    first match in registry order, so the reported reason is `systems`, but
    releasing only `systems` still leaves `architect` holding it.
    """
    from jobscrape.filters import is_excluded_role

    assert is_excluded_role("Systems Architect") == "systems"
    assert is_software_role("Systems Architect") is False
    assert is_software_role("Systems Architect", allow_roles={"systems"}) is False
    assert is_software_role("Systems Architect", allow_roles={"architect"}) is False
    assert is_software_role(
        "Systems Architect", allow_roles={"systems", "architect"}
    ) is True


def test_distributed_systems_is_software_not_systems_engineering() -> None:
    """Backend stays: distributed systems is core backend work, not the
    systems-engineering discipline being excluded, and the suite asserted this
    before the category existed.

    **Systems programming used to be carved out here too**, on the argument
    that it means OS and compiler work. Measured against the archive it means
    mainframe sysprog, which is administration — so it joined the `systems`
    category with every other "Systems X" title. See
    `test_systems_programmer_is_a_systems_role`.
    """
    from jobscrape.filters import is_excluded_role

    for title in ("Distributed Systems Engineer", "Senior Distributed Systems Engineer",
                  "Senior Software Engineer - Distributed Systems"):
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
        "early_career": "Software Engineering Intern",
        "flight": "Flight Software Engineer",
        "architect": "Data Architect",
        "mainframe": "Mainframe Developer",
        "vendor_platform": "Palantir Data Engineer",
        "industrial": "SCADA Programmer",
        "domain_data_science": "Marketing Data Scientist",
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


# --- what a wider board reach exposed --------------------------------------
#
# Going from ~17,000 live boards to 32,910 brought in retailers, grocers,
# hospital systems, pharmaceutical companies and semiconductor fabs — employers
# whose vocabulary overlaps software's without meaning any of the same things.
# Three accept patterns that were harmless while every board belonged to a tech
# company were admitting ~490 of 52,089 open postings between them.


@pytest.mark.parametrize(
    "title",
    [
        # `(back|front|full)[\s-]?end` used to match with no role noun at all.
        # These are the real titles it was admitting, in descending volume.
        "Pharmacy Technician Back End - Full Time",          # 111, a pharmacy chain
        "Teammate Endzone & Loyalty (Front End)",            # 66, a sporting-goods retailer
        "Advisor - Endzone and Loyalty (Front End)",
        "Team Captain Endzone & Loyalty (Front End)",
        "FRONT END/CASHIER",
        "FRONT END/ASST DEPT LEADER",
        "Front End Team Leader",                             # 32, a discount retailer
        "Front End Sales Associate / Cashier",
        "Machine Operator US- Back End Days",
        "Behavioral Health Specialist (FT, Back End Nights)",
    ],
)
def test_front_and_back_end_need_a_role_noun(title: str) -> None:
    """"Front end" is a software tier only inside a software title.

    In a shop it is the checkout; on a production line the back end is the far
    end of the line; in a pharmacy it is the dispensing counter. The bare term
    was the sole thing keeping 492 such postings.
    """
    assert is_software_role(title) is False, title


@pytest.mark.parametrize(
    "title",
    [
        "Front End Engineer",
        "Front-End Developer",
        "Frontend Developer",
        "Senior Backend Developer",
        "Java Backend Engineer",
        "Back-End Software Engineer",
        "Front End Web Developer",
        "Frontend Engineering Lead",
        "Full Stack Engineer",
        "Engineer, Frontend",           # the noun-first spelling
    ],
)
def test_front_and_back_end_still_qualify_with_one(title: str) -> None:
    """The half that must not be lost — these are why the term exists."""
    assert is_software_role(title) is True, title


@pytest.mark.parametrize(
    "title",
    [
        # The blocklist reads discipline-then-noun, so it caught "ASIC Design
        # Engineer" and sailed past the same job written the other way round.
        # These qualified on the accept side's bare seniority+noun rule, which
        # asks nothing about the discipline.
        "Principal Engineer, ASIC Design",
        "Principal Engineer, Design Verification",
        "Staff Engineer, SRAM Circuit Design",
        "Principal/Sr. Principal Engineer - Structural Analysis",
        # Qualifiers stack in chip titles: three words between the discipline
        # and the noun, one more than the two-word gap allows.
        "ASIC Design and Integration Engineer",
        # `mobile engineer` is a bare accept, and building maintenance uses it.
        "Mobile Engineer - Multi-Site Facilities",
    ],
)
def test_a_hardware_discipline_after_the_role_noun_is_blocked(title: str) -> None:
    assert is_software_role(title) is False, title


@pytest.mark.parametrize(
    "title",
    [
        # Lab work reaches the allowlist through the word "Scientist": both
        # `research scientist` and `principal scientist` accept it on seniority
        # alone, with nothing said about the field.
        "Principal Scientist, Bioassay Methods Development",
        "Research Scientist - Analytical Development",
        "Principal Scientist, Upstream Process, Process Development",
        "Principal Scientist - Chemical Biology & Proximity-Based Degraders",
        "Associate Staff Scientist - Clinical Variant Curator",
        "Senior or Principal Scientist, Formulation and Process",
    ],
)
def test_wet_lab_science_is_not_software(title: str) -> None:
    assert is_software_role(title) is False, title


@pytest.mark.parametrize(
    "title",
    [
        # The exemption, doing the same work it does for hardware. A biotech
        # employs software engineers; the test is the discipline in the title,
        # never the industry of the employer.
        "Robotics Software Engineer - Cell Therapy Automation",
        "Software Engineer, Genomics Platform",
        # A bare "Research Scientist" at an AI lab is exactly the posting this
        # archive wants, and it is spelled identically to the pharma one. Only
        # a named laboratory technique separates them.
        "Research Scientist",
        "Machine Learning Research Scientist",
        "Principal Research Scientist",
        # Titles inside suspect wording that are nonetheless real software.
        "Frontend Engineer (Civil Aviation)",
        "Staff Software Engineer - Payroll AI",
        "Silicon Validation Software Engineer",
        "Mobile Engineer",
        "iOS Developer",
    ],
)
def test_the_software_word_survives_all_of_it(title: str) -> None:
    assert is_software_role(title) is True, title


@pytest.mark.parametrize(
    "title",
    [
        # Every one of these was a REAL posting the first draft of the rules
        # above threw away. They are here because reading the drop list against
        # the live archive is the only thing that found them — the unit cases
        # were all green.
        #
        # A trailing clause names the domain served, not the discipline
        # practised, whenever the title already named a software role.
        "Forward Deployed Engineer - Aerospace",
        "Frontend Engineer (Civil Aviation)",
        # `soc` is a chip word AND the Security Operations Centre.
        "Senior Backend Engineer, SOC & Threat Hunting",
        # Five hardware disciplines are also software product names.
        "Java Developer - with Polymer JS Experience",
        # "Data / Cloud Native Engineer - Palantir Foundry Experience" was here
        # too, rescued when a bare `foundry` in the hardware rule was wrongly
        # catching Palantir's product. It is now dropped ON PURPOSE by the
        # `vendor_platform` category — the same title, first a false positive
        # and later a deliberate exclusion. Kept as a comment because the
        # hardware-rule fix it guards is still live: see
        # test_the_tightenings_that_measurement_forced, where NXP's
        # semiconductor "Foundry Operations" role stands in for it.
        # The stack sits between the tier and the noun.
        "Back-End Java Spring Boot Engineer - Banking Sector",
        "Senior Developer 1 - Front End",
        # The closed-up spelling is a software signal on its own: measured
        # across 52,205 open postings it carries a retail or shift marker in 26
        # of 4,066 titles, against 195 of 1,253 for the spaced form.
        "Frontend Team Lead",
        "Backend Studio Lead (.NET)",
        # Software roles in life sciences are named after their own discipline,
        # so the word "software" is not there to rescue them.
        "Senior Data Engineer - Digital Pathology (Hybrid)",
        "AI Scientist - Image Analysis & Digital Pathology",
    ],
)
def test_real_postings_the_first_draft_wrongly_dropped(title: str) -> None:
    assert is_software_role(title) is True, title


# --- architect: the tenth preference category ------------------------------


ARCHITECT_TITLES = [
    "Data Architect",
    "Cloud Architect",
    "AI Architect",
    "Technical Architect",
    "Software Architect",
    "Integration Architect",
    "Application Architect",
    "Infrastructure Architect",
    "Principal Data Architect",
    "Controls Software Architect, Steward Observatory",
]


@pytest.mark.parametrize("title", ARCHITECT_TITLES)
def test_architects_are_excluded_by_preference(title: str) -> None:
    """1,342 open postings, and the whole word rather than a curated subset.

    Three architect senses were already excluded through other categories —
    `Systems Architect` via `systems`, `Network Architect` via `network`,
    `Solutions Architect` via `_NOT_SOFTWARE_TERMS` — so a partial rule would
    have left an arbitrary half of them with no way to describe which half.
    """
    from jobscrape.filters import is_excluded_role

    assert is_excluded_role(title) == "architect", title
    assert is_software_role(title) is False, title


@pytest.mark.parametrize("title", ARCHITECT_TITLES)
def test_include_roles_architect_restores_every_one(title: str) -> None:
    """The check a half-working flag fails.

    Excluding these while ALSO removing their accept patterns would look
    identical from the default path and quietly restore nothing — the exact
    shape `_SOFTWARE_TERMS` warns about in three separate comments. So the
    accepts stay, and this asserts they do.
    """
    assert is_software_role(title, allow_roles={"architect"}) is True, title


@pytest.mark.parametrize(
    "title",
    ["Systems Programmer", "Senior Systems Programmer", "Systems Programmer I"],
)
def test_systems_programmer_is_a_systems_role(title: str) -> None:
    """`_SOFTWARE_SYSTEMS_RE` used to rescue this from the `systems` category,
    arguing that systems programming means OS and compiler work. Measured
    against the archive it means mainframe sysprog, which is administration —
    the same thing every other "Systems X" title in that category is."""
    from jobscrape.filters import is_excluded_role

    assert is_excluded_role(title) == "systems", title
    assert is_software_role(title) is False, title
    # Same round-trip: the accept pattern stays so the flag means something.
    assert is_software_role(title, allow_roles={"systems"}) is True, title


def test_distributed_systems_is_still_rescued() -> None:
    """The arm of `_SOFTWARE_SYSTEMS_RE` that stays, and has its own reason:
    distributed systems is core backend work, not systems engineering."""
    assert is_software_role("Distributed Systems Engineer") is True


@pytest.mark.parametrize(
    "title",
    [
        # A slash, not a space — the rule demanded whitespace between the
        # qualifier and the noun, which is the third separator assumption to
        # cost real rows this week.
        "Senior CNC Machinist/Programmer",
        # Computer-aided manufacturing, and audio-visual system programming.
        "CAM Programmer",
        "Sr. AV Programmer",
        # Clinical-trial biostatistics: a distinct profession that writes SAS.
        "SAS Programmer",
        "Principal Statistical Programmer",
        "Senior Clinical Programmer I",
        "Senior Statistical Programmer",
    ],
)
def test_more_non_software_programmers(title: str) -> None:
    assert is_software_role(title) is False, title


# --- blocked employers ------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Amazon", "amazon", "Microsoft", "Microsoft Corporation",
        "Apple", "Apple Inc.", "Coinbase", "Airbnb", "Airbnb, Inc.",
        "UnitedHealth Group", "United Health Group", "UnitedHealth",
        "Stripe", "PayPal", "Paypal", "paypal",
        # A Workday board slug: only the tenant names the company.
        "paypal|wd1|jobs",
    ],
)
def test_blocked_employers_are_recognised_in_every_spelling(name: str) -> None:
    """Both sides normalise, so case, punctuation and legal suffixes do not
    matter — `slugify` strips "Inc." and "Corporation" before comparison."""
    from jobscrape.filters import is_blocked_company

    assert is_blocked_company(name) is True, name


@pytest.mark.parametrize(
    "name",
    [
        # Every one of these is a REAL board in the company registry, with the
        # posting count it was carrying when the blocklist was written. A
        # prefix rule would have taken all of them.
        "apple-roofing",              # ashby, 58 postings
        "appletreedental",            # lever, 50
        "applewoodcenters",           # jazzhr, 34
        "applebank|wd5|applebankcareers",  # workday, 20
        "appletreeprep",              # greenhouse, 10
        "applepiecapital",
        "appleseed",
        "apple-restoration-and-waterproofing",
        "microsoftc1",                # smartrecruiters, 6
        "stripes",                    # greenhouse — not Stripe
        # Subsidiaries and brands, deliberately NOT blocked: the list is the
        # eight exact names, so these keep coming in under their own.
        "UnitedHealthcare", "Optum", "Amazon Advertising", "Amazon Robotics",
        "Venmo", "LinkedIn", "GitHub", "AWS",
    ],
)
def test_look_alikes_and_subsidiaries_are_not_blocked(name: str) -> None:
    from jobscrape.filters import is_blocked_company

    assert is_blocked_company(name) is False, name


def test_the_env_var_extends_the_list(monkeypatch) -> None:
    """You will add employers over time, and editing .env beats editing code."""
    from jobscrape import config, filters

    assert filters.is_blocked_company("Globex") is False
    monkeypatch.setattr(config, "BLOCKED_COMPANIES", ("Globex Corporation",))
    assert filters.is_blocked_company("Globex") is True
    # And the built-in eight are still blocked alongside it.
    assert filters.is_blocked_company("Coinbase") is True


def test_a_blocked_posting_is_attributed_to_the_company_stage() -> None:
    """First in the funnel, because it is the most specific reason a posting can
    be dropped — reporting an Amazon posting as "not software" answers a
    question nobody asked."""
    from jobscrape.filters import apply_filters_counted
    from jobscrape.models import Job

    def job(company: str, title: str = "Software Engineer"):
        return Job(source="adzuna", source_job_id=company, title=title, company=company,
                   job_url="https://x/1", location_raw="Austin, TX")

    kept, funnel = apply_filters_counted(
        [job("Amazon"), job("Apple Roofing"), job("Acme"),
         # Fails role AND company: company wins, because it runs first.
         job("Stripe", "Front End Team Leader")],
        # This test is about funnel attribution order, not workplace_type.
        remote_only=False,
    )
    assert [k.company for k in kept] == ["Apple Roofing", "Acme"]
    assert funnel.dropped_company == 2
    assert funnel.dropped_role == 0
    d = funnel.as_dict()
    assert (d["dropped_company"] + d["dropped_role"] + d["dropped_location"]
            + d["dropped_window"] + d["dropped_workplace"] + d["kept"]) == d["seen"]


def test_the_deleted_employer_adapters_are_gone() -> None:
    """Amazon and Apple had hand-written adapters that existed only to fetch
    companies now on the blocklist — ~4,700 requests a sweep for postings the
    filter would immediately discard."""
    from jobscrape.sources import ALL_SOURCE_NAMES, GLOBAL_SOURCES

    assert "amazon" not in GLOBAL_SOURCES and "amazon" not in ALL_SOURCE_NAMES
    assert "apple" not in GLOBAL_SOURCES and "apple" not in ALL_SOURCE_NAMES
    assert "uber" in GLOBAL_SOURCES, "Uber is not blocked and stays"


# --- three more preference categories, and three more blocklist entries -----
#
# All six came from one list of titles that were still getting through. What
# they have in common is that each reached the accept list honestly — through
# `data science` in the subject a professor teaches, `software engineer` in the
# title of a Workday integrator, `product engineer` on a semiconductor role.


@pytest.mark.parametrize(
    "title,category",
    [
        ("Mainframe Developer (COBOL)", "mainframe"),
        ("Junior Mainframe Developer (COBOL)", "mainframe"),
        ("Senior Mainframe Storage Engineer", "mainframe"),
        ("Software Engineer Lead, COBOL, Systematics", "mainframe"),
        ("IMS Application DBA", "mainframe"),
        ("IMS Database Administrator", "mainframe"),

        # Not the vendors as employers — that is BLOCKED_COMPANIES. This is the
        # consultancy implementing their product: all 19 Palantir postings
        # measured were Deloitte, Booz Allen, Guidehouse, L3Harris or Endava.
        ("Palantir Data Engineer", "vendor_platform"),
        ("Forward Deployed Engineer (Palantir)", "vendor_platform"),
        ("Data Engineer - Palantir Foundry", "vendor_platform"),
        ("PeopleSoft Application Developer", "vendor_platform"),
        ("Principal Software Engineer (Oracle Fusion)", "vendor_platform"),
        ("Software Engineer III - ServiceNow", "vendor_platform"),
        ("Sr. SAP Commerce Platform Engineer", "vendor_platform"),
        ("SENIOR SOFTWARE ENGINEER - Workday", "vendor_platform"),
        ("Integration Engineer (Epic Integration Specialist)", "vendor_platform"),

        ("SCADA Programmer", "industrial"),
        ("Software Engineer, SCADA", "industrial"),
        ("Flexsort Software Engineer", "industrial"),
    ],
)
def test_the_three_new_categories(title: str, category: str) -> None:
    from jobscrape.filters import is_excluded_role

    assert is_excluded_role(title) == category, title
    assert is_software_role(title) is False, title


@pytest.mark.parametrize(
    "title,category",
    [
        ("Mainframe Developer (COBOL)", "mainframe"),
        ("Software Engineer Lead, COBOL, Systematics", "mainframe"),
        ("Palantir Data Engineer", "vendor_platform"),
        ("Principal Software Engineer (Oracle Fusion)", "vendor_platform"),
        ("Software Engineer III - ServiceNow", "vendor_platform"),
        ("Software Engineer, SCADA", "industrial"),
        ("Flexsort Software Engineer", "industrial"),
    ],
)
def test_the_new_categories_round_trip_through_their_flag(title: str, category: str) -> None:
    """The check a half-working flag fails.

    `\\bmainframe\\s+(engineer|developer|programmer)` and `\\bcobol\\b` are in the
    ACCEPT list on purpose and stay there; the others qualify through `software
    engineer` and `data engineer`. Either way `--include-roles` has to give them
    back, or the flag is decoration.
    """
    assert is_software_role(title, allow_roles={category}) is True, title


@pytest.mark.parametrize(
    "title",
    [
        # Both of these were found by measuring the draft rules against the
        # archive, not by thinking about them.
        #
        # `db2` and `z/os` matched one title three times — an ordinary data
        # engineer listing every database they might touch. `z/os` contributed
        # nothing else whatsoever: its only other match was already caught by
        # the word "mainframe".
        "Data Engineer II - DB2 z/OS, Oracle, SQL Server, PostgreSQL, or Netezza",
        # A bare `foundry` in the vendor-platform rule caught a SEMICONDUCTOR
        # foundry, which is the opposite kind of job.
        "APAC Foundry Operations Senior Principal Engineer",
        # `plc` is absent from `industrial` for the same reason: this is a
        # software job that happens to talk to a controller.
        "Senior Software Engineer - C# / PLC (m/f/d)",
        # `epic` is guarded — Epic Games is a software employer, Epic Systems
        # is the EHR whose integrators are the target.
        "Senior Software Engineer, Epic Games",
    ],
)
def test_the_tightenings_that_measurement_forced(title: str) -> None:
    assert is_software_role(title) is True, title


@pytest.mark.parametrize(
    "title",
    [
        "Adjunct Faculty (Data Science and Artificial Intelligence)",
        "Lecturer, LIS/Data Science/Creative Computing",
        "Research Scientist, Assist/Assoc/Full Professor",
        # Bootcamps too: teaching software engineering is a teaching job
        # whoever employs you.
        "Software Engineer Instructor",
        "Software Engineering Instructor Lead",
        "Lead Instructor: GenAI",
        # On-site hardware repair.
        "Field Service Engineer - AIS",
        "Field Service Engineer - Airports Security & Audio Systems",
        # Semiconductor, reached through the bare `product engineer` term.
        "Electron Beam Product Engineer",
        "Business Development Manager",
    ],
)
def test_teaching_field_service_and_e_beam_are_not_software(title: str) -> None:
    """These are in the blocklist rather than the preference registry: they are
    not software jobs under any preference, which is the line that list draws."""
    assert is_software_role(title) is False, title


def test_field_service_needs_its_role_noun() -> None:
    """"Field Service Software Engineer" writes software FOR a field-service
    operation, which is why the rule names the noun instead of the phrase."""
    assert is_software_role("Field Service Software Engineer") is True


# --- what a full audit of the archive turned up -----------------------------
#
# Every one of these arrived through an accept term that is correct in general
# and unqualified in particular — `research engineer`, `product engineer`,
# `integration engineer`, `data scientist`. None was a filter bug; all were
# missing qualifiers, and they are here so they stay closed.


@pytest.mark.parametrize(
    "title",
    [
        # Bench research in a hardware discipline. These survived only because
        # the hardware rules named engineers and developers but never
        # SCIENTISTS, whichever side of the noun the discipline sat.
        "Optics Research Scientist",
        "Research Scientist, Catalyst Simulation",
        "Radar Research Engineer",
        "Senior Research Engineer in FEA Modeling",
        "Finite Element (FEA) Research Engineer",
        # Semiconductor, through `product engineer` and `integration engineer`.
        "Senior DRAM Product Engineer",
        "Sr Process Integration Engineer, DRAM",
        "Wafer Probe Integration Engineer",
        # Manufacturing.
        "Senior Injection Molding Tooling Engineer",
        "Product Engineer (Chemicals)",
        # Health-economics and outcomes research, through `research scientist`.
        "Senior Research Scientist, Evidence Synthesis",
        "Associate Research Scientist, Evidence Generation",
        "Research Scientist, Health Economic Modeling",
        # A thesis placement is not a job opening.
        "GenAI Research Student Worker",
        "Master's Thesis in the Field of Data Science / Machine Learning",
    ],
)
def test_the_leaks_the_audit_found(title: str) -> None:
    assert is_software_role(title) is False, title


@pytest.mark.parametrize(
    "title",
    [
        # The `_names_software` exemption, still doing its job — both verified
        # against the live archive before the discipline words went in.
        "Radar Software Engineer",
        "Senior Radar Software Engineer",
        "Electron Optics Devices Software Engineer",
        # Bare research roles are AI research until a discipline says otherwise.
        "Research Scientist",
        "Research Engineer",
        "AI Research Engineer",
        "Research Engineer - Post-Training",
        "Machine Learning Research Engineer",
        # Bare product engineering is software product engineering.
        "Product Engineer",
        "AI Product Engineer",
        "Full Stack Product Engineer",
        # `_UNAMBIGUOUS_SOFTWARE_RE` now carries the case that brackets used to
        # be excluded for — see test_brackets_are_no_longer_load_bearing.
        "Frontend Engineer (Civil Aviation)",
        "Forward Deployed Engineer - Aerospace",
        # False alarms from the audit's own keyword sweep, kept as a reminder
        # that the domain in a title is usually not the discipline.
        "AI Tooling Engineer",
        "Staff Frontend Engineer, DC Infrastructure Tooling",
        "Release Engineer, Warehouse Transformation",
        "Staff Machine Learning Engineer, Driver Understanding and Evaluation",
        "Senior Software Engineer, Backend - HR Product",
    ],
)
def test_the_audit_did_not_overreach(title: str) -> None:
    assert is_software_role(title) is True, title


def test_brackets_are_no_longer_load_bearing() -> None:
    """The reversed hardware rule excluded brackets from its separator class so
    that "Frontend Engineer (Civil Aviation)" would survive.

    That was punctuation standing in for meaning, and it cost "Product Engineer
    (Chemicals)". `_UNAMBIGUOUS_SOFTWARE_RE` was added later and decides the
    same case on the right grounds — does the title name a software role — so
    brackets could join the class and both titles now come out correctly.
    """
    assert is_software_role("Frontend Engineer (Civil Aviation)") is True
    assert is_software_role("Product Engineer (Chemicals)") is False


DOMAIN_DATA_SCIENCE = [
    "Marketing Data Scientist",
    "Sr. Data Scientist, Performance Marketing",
    "Chemical Data Scientist",
    "Senior Clinical Data Scientist",
    "Data Scientist, Supply Chain",
    "Data Scientist Senior (Population Health)",
    "Senior Data Scientist / Risk Modeling Analyst",
    "Data Scientist, Finance",
]


@pytest.mark.parametrize("title", DOMAIN_DATA_SCIENCE)
def test_domain_qualified_data_science_is_excluded(title: str) -> None:
    """A deliberate choice that costs real software work, recorded as such.

    "Staff Data Scientist, Marketing Inference" is ordinary ML whose qualifier
    names the industry served, exactly as "Data Engineer, Payments" does. The
    domain list is fixed and short so the blast radius stays the 59 measured.
    """
    from jobscrape.filters import is_excluded_role

    assert is_excluded_role(title) == "domain_data_science", title
    assert is_software_role(title) is False, title


@pytest.mark.parametrize("title", DOMAIN_DATA_SCIENCE)
def test_domain_data_science_round_trips(title: str) -> None:
    assert is_software_role(title, allow_roles={"domain_data_science"}) is True, title


@pytest.mark.parametrize(
    "title",
    ["Data Scientist", "Senior Data Scientist", "Staff Data Scientist",
     "Data Scientist, Trust & Safety", "Data Engineer, Payments"],
)
def test_undomained_data_science_is_untouched(title: str) -> None:
    assert is_software_role(title) is True, title


# --- four ways a title dodged the discipline rules --------------------------
#
# The first purge left 13 rows behind, and re-running the audit against the
# archive is what found them. Each was a mechanical gap in how the patterns
# were written, not a missing discipline — which is why they are worth pinning.


@pytest.mark.parametrize(
    "title",
    [
        # The gap counted only `\w+` tokens joined by whitespace, so an
        # ampersand, slash or comma inside it broke the chain.
        "Injection Molding & Tooling Engineer",
        "Mechanical, Materials, Chemical & Robotics Engineer",
        "Senior Tooling Engineer (Injecting Molds / Molding)",
        # `\bengineer\b` never matched "EngineerING".
        "Principal, Molding & Tooling Engineering",
        "GhostEye Radar System Engineering Cross Product Lead",
        # The discipline sat at one end and the role noun at the other, past
        # any reasonable gap — which is why radar joined the any-distance set
        # alongside ASIC.
        "Space RADAR Payload I&T SME - Sr. Staff Engineer Systems",
        "Space RADAR Data Processing - Principal/Sr Principal Engineer",
        # No engineer/developer/scientist noun at all.
        "Principal Radar Backend Electronics Lead",
        # A CURLY apostrophe. The posting is titled "Master’s Thesis…" and the
        # pattern spelled a straight one.
        "Master’s Thesis in the Field of Data Science / Machine Learning",
    ],
)
def test_the_edge_cases_the_first_purge_missed(title: str) -> None:
    assert is_software_role(title) is False, title


def test_widening_the_gap_stayed_out_of_the_other_categories() -> None:
    """`_HW_GAP` exists rather than widening `_GAP` itself.

    `_GAP` is shared with `_NETWORK_RE` and `_SYSTEMS_RE`. Widening it to allow
    a slash made the `systems` category swallow "Radar Systems/Software
    Engineer" — a software job, held by a rule this change was not trying to
    touch. The hardware rules got their own gap instead.
    """
    assert is_software_role("Radar Systems/Software Engineer") is True
    assert is_software_role("Injection Molding & Tooling Engineer") is False
    # And the systems category still behaves as it did.
    assert is_software_role("Distributed Systems Engineer") is True
    assert is_software_role("Systems Engineer") is False
