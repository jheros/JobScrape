"""Seniority classification from the job title.

The ladder is evaluated top to bottom and the first match wins, so almost every
test here is really a test of *ordering*. 5.8% of real titles name more than one
level, and those collisions are where a plausible-looking implementation gets it
wrong.
"""

from __future__ import annotations

import pytest

from jobscrape.models import Job
from jobscrape.seniority import LEVELS, UNKNOWN, classify, sort_key


@pytest.mark.parametrize(
    "title,expected",
    [
        # --- unambiguous ---
        ("Senior Software Engineer", "senior"),
        ("Sr. Backend Developer", "senior"),
        ("Staff Engineer", "staff"),
        ("Principal Engineer", "principal"),
        ("Distinguished Engineer", "principal"),
        ("Engineering Manager", "manager"),
        ("Director of Engineering", "director"),
        ("VP of Engineering", "executive"),
        ("Head of Platform", "executive"),
        ("Software Engineer Intern", "intern"),
        ("Backend Engineering Co-op", "intern"),
        ("Junior Developer", "entry"),
        ("New Grad Software Engineer", "entry"),
        ("Associate Software Engineer", "entry"),
        ("Tech Lead, Payments", "lead"),
        # "Distinguished Fellow" still correctly lands here via "distinguished"
        # alone — only the bare "fellow" alternative was removed.
        ("Distinguished Fellow", "principal"),
        # --- nothing stated: the honest answer ---
        ("Software Engineer", UNKNOWN),
        ("Backend Developer", UNKNOWN),
        ("Data Scientist", UNKNOWN),
        ("", UNKNOWN),
        # "Fellow"/"Fellowship" is heavily used for early-career and
        # rotational programs, not just the Distinguished-Engineer-adjacent
        # sense — a bare "fellow" used to land in the "principal" tier,
        # misreading these as senior-IC titles.
        ("Software Engineering Fellow", UNKNOWN),
        ("AI Residency Fellow", UNKNOWN),
    ],
)
def test_single_signal(title: str, expected: str) -> None:
    assert classify(title) == expected


@pytest.mark.parametrize(
    "title,expected,why",
    [
        ("Senior Staff Engineer", "staff",
         "two IC rungs: the higher one wins"),
        ("Principal Engineering Manager", "manager",
         "people-management outranks the IC word, which describes what they manage"),
        ("Senior Engineering Manager", "manager", "same"),
        ("Software Engineer Intern, Senior Platform Team", "intern",
         "the team's name is not the candidate's level"),
        ("Lead Software Engineer II", "lead",
         "an explicit word beats a numeric suffix"),
        ("Senior Software Engineer II", "senior", "same"),
        ("Staff Engineer, Lead for Payments", "staff",
         "staff outranks a scope-describing 'lead'"),
        ("Director, Senior Engineering Programs", "director",
         "director outranks senior"),
        ("VP, Principal Architecture Group", "executive",
         "executive outranks principal"),
    ],
)
def test_precedence(title: str, expected: str, why: str) -> None:
    assert classify(title) == expected, why


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Software Engineer I", "entry"),
        ("Software Engineer 1", "entry"),
        ("Software Engineer II", "mid"),
        ("Software Engineer III", "mid"),
        ("Data Scientist 3", "mid"),
        ("Software Engineer IV", "senior"),
        ("Systems Engineer V", "senior"),
        # ATSes append the team name after the level. Anchoring only to the end
        # of the string missed 408 real titles in the archive.
        ("Software Engineer II, Backend", "mid"),
        ("Software Engineer I, Data Platform", "entry"),
        ("Software Engineer III - Device Drivers", "mid"),
        ("NOC Engineer III, Incident Management Team", "mid"),
        ("Software Engineer Level 2 - FFPP", "mid"),
    ],
)
def test_numeric_level_suffixes(title: str, expected: str) -> None:
    assert classify(title) == expected


def test_numeric_suffix_must_end_the_title_or_precede_a_separator() -> None:
    """A stray numeral mid-title is not a level.

    Anchoring matters: without it "Section 8 Housing Systems Engineer" reads as
    a mid-level role because of the "8", and roman numerals appear in plenty of
    programme names.
    """
    assert classify("Section 8 Housing Systems Engineer") == UNKNOWN
    assert classify("Engineer, Apollo II Program") == UNKNOWN
    # A numeral followed by more words is not a level; only end-of-string or a
    # comma/dash separator counts.
    assert classify("Engineer for Terminal 2 Operations") == UNKNOWN


def test_no_partial_word_matches() -> None:
    """The ladder is word-boundary anchored throughout.

    "Sriracha" must not read as "sr.", and "Internal Tools Engineer" must not
    read as an internship — that second one would misclassify a real class of
    posting.
    """
    assert classify("Internal Tools Engineer") == UNKNOWN
    assert classify("International Systems Developer") == UNKNOWN
    assert classify("Interface Engineer") == UNKNOWN
    # "Leadership Development Engineer" is a role in a leadership *programme*,
    # not a lead engineer — and `\blead\b` correctly declines to match inside
    # "leadership".
    assert classify("Leadership Development Engineer") == UNKNOWN
    # But a real lead still matches.
    assert classify("Lead Platform Engineer") == "lead"


def test_none_and_whitespace() -> None:
    assert classify(None) == UNKNOWN
    assert classify("   ") == UNKNOWN


def test_every_result_is_a_declared_level() -> None:
    """Guards against a ladder entry whose name isn't in LEVELS."""
    titles = [
        "Senior Engineer", "Staff Engineer", "Principal Engineer", "Intern",
        "VP Engineering", "Director", "Manager", "Lead", "Junior", "Engineer II",
        "Engineer I", "Engineer IV", "Software Engineer",
    ]
    assert {classify(t) for t in titles} <= set(LEVELS)


def test_sort_key_orders_junior_to_senior() -> None:
    ladder = ["intern", "entry", "mid", "senior", "staff", "principal"]
    assert sorted(ladder, key=sort_key) == ladder
    # Unknown sorts last, so it never leads a grouped view.
    assert sort_key(UNKNOWN) > sort_key("executive")
    assert sort_key("not-a-level") >= sort_key(UNKNOWN)


# --- integration with the Job model ----------------------------------------


def test_job_derives_seniority_from_its_title() -> None:
    job = Job(source="t", source_job_id="1", title="Staff Backend Engineer",
              company="Acme", job_url="https://x/1")
    assert job.seniority == "staff"


def test_an_adapter_supplied_level_is_not_overwritten() -> None:
    """Same contract `workplace_type` has with `workplace_hint`: the derivation
    only fills a blank, so a source that states the level outright wins."""
    job = Job(source="t", source_job_id="1", title="Software Engineer",
              company="Acme", job_url="https://x/1", seniority="principal")
    assert job.seniority == "principal"


def test_seniority_survives_a_shallow_refresh() -> None:
    """It recomputes from the title, which a shallow refresh keeps — which is
    why, unlike skills, it needs no preservation guard in the upsert."""
    from jobscrape.sources.base import shallow_refresh

    job = Job(source="t", source_job_id="1", title="Senior Data Engineer",
              company="Acme", job_url="https://x/1",
              description_text="Long description here.")
    refreshed = shallow_refresh(job)
    assert refreshed.description_text is None, "the description is stripped"
    assert refreshed.seniority == "senior", "the level is not"
