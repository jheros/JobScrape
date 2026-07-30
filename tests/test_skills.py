"""Skill extraction from job descriptions.

**The rejection cases are the point of this file.** Every string in
`test_ordinary_english_is_not_a_skill` is taken from a real posting that a
naive, non-word-boundary implementation flagged, and each one represents a
measured false-positive rate on the live archive:

* ``rust`` matched 1,677 of 4,000 descriptions without boundaries and 305 with
  — 82% of the difference was *trustworthiness*, *trust*, *trusted*, *thrust*.
* ``scala`` matched 1,543 vs 137 — 91% was *scalable*, *scalability*,
  *escalation*.
* ``excel`` matched 253, of which 132 of the top 141 were the verb ("a culture
  where everyone can excel").
* ``spark`` matched 254, of which 38 of the top 100 were "spark innovation".

Those four are why the vocabulary is closed and every pattern is anchored. If
one of these tests starts failing, the column is filling with noise.
"""

from __future__ import annotations

import pytest

from jobscrape.skills import (
    MAX_SCAN_CHARS, VOCABULARY, extract, parse_field, to_field,
)


# --- real mentions are found -----------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Strong Python and SQL experience required.", {"python", "sql"}),
        ("You will write Go and Rust services.", {"rust"}),   # bare "Go" is unusable
        ("Experience with Golang microservices.", {"go", "microservices"}),
        ("We run Kubernetes on AWS with Terraform.", {"kubernetes", "aws", "terraform"}),
        ("Deep React and TypeScript skills.", {"react", "typescript"}),
        ("Building with PyTorch and Hugging Face transformers.",
         {"pytorch", "hugging face"}),
        ("Postgres, Redis and Kafka in production.", {"postgres", "redis", "kafka"}),
        ("Proficiency in Swift and SwiftUI on iOS.", {"swift", "swiftui", "ios"}),
    ],
)
def test_real_mentions(text: str, expected: set[str]) -> None:
    assert expected <= set(extract(text))


# --- ordinary English is not a skill ---------------------------------------


@pytest.mark.parametrize(
    "text,must_not_contain",
    [
        # The four measured offenders.
        ("We value trustworthiness and a trusted, trustworthy team.", "rust"),
        ("The main thrust of the role is delivery.", "rust"),
        ("Build scalable systems with strong scalability.", "scala"),
        ("Handle escalations and escalate blockers early.", "scala"),
        ("A culture where everyone can excel.", "excel"),
        ("You will have every opportunity to excel here.", "excel"),
        ("We look for people who excel at solving hard problems.", "excel"),
        ("Help spark innovation across the org.", "spark"),
        # Bare "go" — 1,118 of 4,000 descriptions contain the ordinary verb.
        ("You will go to market with our sales team.", "go"),
        ("Let's go build something great.", "go"),
        # Bare "R" — 441 of 4,000, almost all stray capitals.
        ("R&D team based in Boston.", "r"),
        ("Reports to the VP. R E M O T E friendly.", "r"),
        # `java` must not fire on `javascript`.
        ("Modern JavaScript tooling.", "java"),
        # `git` must not fire on GitHub/GitLab alone.
        ("Post your work to GitHub.", "git"),
    ],
)
def test_ordinary_english_is_not_a_skill(text: str, must_not_contain: str) -> None:
    assert must_not_contain not in extract(text), (
        f"{must_not_contain!r} matched ordinary prose: {text!r}"
    )


def test_the_measured_inversion_stays_fixed() -> None:
    """`rust` and `scala` outranking `java` was how the bug surfaced.

    A description mentioning trust and scalability but no such language must
    yield neither.
    """
    prose = (
        "We are building a trusted, trustworthy platform. Scalability and "
        "scalable architecture are core values, and we escalate issues fast. "
        "Our stack is Java and Spring Boot."
    )
    found = extract(prose)
    assert "java" in found
    assert "rust" not in found
    assert "scala" not in found


# --- ambiguous short names --------------------------------------------------


def test_go_needs_an_unambiguous_form() -> None:
    assert "go" in extract("Written in Golang.")
    assert "go" in extract("Hiring a Go developer.")
    assert "go" in extract("Services written in Go and deployed daily.")
    assert "go" not in extract("Ready to go the extra mile?")


def test_r_needs_a_statistical_context() -> None:
    assert "r" in extract("Modelling in R and Python.")
    assert "r" in extract("Comfortable in R programming.")
    assert "r" not in extract("R&D budget is generous.")
    assert "r" not in extract("Reports to R. Smith.")


def test_c_family_are_distinguished() -> None:
    assert "c++" in extract("Systems work in C++.")
    assert "c#" in extract("Backend in C# and .NET.")
    assert "c" in extract("Low-level C/C++ experience.")
    # A bare capital C is an initial far more often than the language.
    assert "c" not in extract("Reports to C. Jones in the NYC office.")


# --- aliasing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text,canonical",
    [
        ("Experience with k8s.", "kubernetes"),
        ("Deploy to Kubernetes.", "kubernetes"),
        ("PostgreSQL tuning.", "postgres"),
        ("We use Postgres.", "postgres"),
        ("Amazon Web Services certified.", "aws"),
        ("AWS certified.", "aws"),
        ("Node.js backend.", "node"),
        ("Familiar with sklearn.", "scikit-learn"),
        ("Large language models in production.", "llm"),
    ],
)
def test_aliases_collapse_to_one_name(text: str, canonical: str) -> None:
    assert canonical in extract(text)


# --- storage round-trip -----------------------------------------------------


def test_output_is_sorted_and_stable() -> None:
    """The stored value is compared on equality, so ordering must not drift."""
    text = "Kubernetes, AWS, Python, Docker"
    assert extract(text) == sorted(extract(text))
    assert extract(text) == extract(text)


def test_to_field_and_parse_field_round_trip() -> None:
    text = "Python, AWS and Kubernetes."
    stored = to_field(text)
    assert isinstance(stored, str)
    assert parse_field(stored) == extract(text)
    # Empty is empty, not [''].
    assert to_field(None) == ""
    assert parse_field("") == []
    assert parse_field(None) == []


def test_scanning_is_bounded() -> None:
    """Unbounded scanning of a 100k-char posting is a self-inflicted stall, and
    the tail of a long description is boilerplate rather than a tech stack."""
    buried = "filler " * MAX_SCAN_CHARS + " we use Kubernetes"
    assert "kubernetes" not in extract(buried)


def test_no_description() -> None:
    assert extract(None) == []
    assert extract("") == []


def test_vocabulary_patterns_all_compile_and_are_anchored() -> None:
    """Every pattern must be boundary-anchored somewhere — an unanchored one is
    how `rust` and `scala` got in."""
    import re

    for name, pattern in VOCABULARY.items():
        re.compile(pattern)  # raises if malformed
        assert r"\b" in pattern or pattern.startswith(r"\."), (
            f"{name!r} has no word boundary: {pattern!r}"
        )
