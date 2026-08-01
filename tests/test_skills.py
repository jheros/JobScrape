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


# --- the prefilter must not change a single answer -------------------------
#
# `extract` skips a pattern entirely unless one of its leading literals is
# present as a plain substring, which is what makes a 722-term vocabulary
# cheaper than the 133-term one was. It is only valid if the literal is
# genuinely REQUIRED, and two plausible ways of deriving it are wrong:
#
#   * the longest literal run picks `framework` out of
#     `\bspring\s*(?:boot|framework|mvc)\b`, where only `spring` is required;
#   * the leading run taken whole picks `llms` out of `\bllms?\b`, where the
#     trailing `s` is optional — that version silently produced 80 wrong
#     answers over 1,500 real descriptions.
#
# So the invariant gets a test rather than a comment.


def test_every_prefilter_literal_is_actually_required() -> None:
    """A pattern's own match must always contain one of its literals.

    Generated from the vocabulary itself, so a new entry is covered the moment
    it is added and cannot quietly opt out.
    """
    import re

    from jobscrape.skills import _COMPILED

    for name, pattern, literals in _COMPILED:
        if not literals:
            continue          # no prefilter — always runs, trivially sound
        for branch in pattern.pattern.split("|"):
            # Build the shortest string this branch can match by stripping the
            # regex syntax it is allowed to contain.
            sample = branch
            for token, replacement in (
                (r"\b", ""), (r"\s+", " "), (r"\s*", " "), (r"\.?", "."),
                (r"\.", "."), (r"\+", "+"), (r"\-", "-"),
            ):
                sample = sample.replace(token, replacement)
            sample = re.sub(r"\(\?[:=!][^)]*\)[?*]?", "", sample)
            sample = re.sub(r"\[[^\]]*\][{?*][^}]*}?", "", sample)
            sample = sample.replace("?", "").replace("*", "")
            if not sample.strip():
                continue
            assert any(literal in sample for literal in literals), (
                f"{name}: branch {branch!r} reduces to {sample!r}, which contains "
                f"none of the prefilter literals {literals} — the prefilter would "
                f"skip a description this branch matches"
            )


def test_every_pattern_is_lowercase() -> None:
    """Patterns are matched against lowercased text, so an uppercase letter in
    one means it can never fire — silently, with no error anywhere."""
    offenders = [
        name for name, pattern in VOCABULARY.items()
        if any(ch.isupper() for ch in pattern)
    ]
    assert not offenders, f"patterns containing uppercase: {offenders}"


def test_the_vocabulary_has_no_duplicate_patterns() -> None:
    """Two names sharing a pattern means one of them can never be the answer."""
    seen: dict[str, str] = {}
    duplicates = []
    for name, pattern in VOCABULARY.items():
        if pattern in seen:
            duplicates.append((seen[pattern], name, pattern))
        seen[pattern] = name
    assert not duplicates, f"duplicate patterns: {duplicates}"


# --- the ambiguous names, measured on 8,000 real descriptions ---------------
#
# Each of these is ordinary English far more often than it is a product, so
# each carries a hand-written pattern requiring product context. The bare-word
# rate on the live archive is in the comment beside each entry in skills.py.


@pytest.mark.parametrize(
    "sentence",
    [
        "Make sure to deliver on time and make an impact.",           # make, 48%
        "The foundation of our platform is solid.",                   # foundation, 11%
        "Less experience is required for this role.",                 # less, 4.8%
        "You will combine multiple data sources into one view.",      # combine, 3.8%
        "What kind of engineer are you?",                             # kind, 3.4%
        "Plenty of room to grow in this role.",                       # room, 2.4%
        "Harness the power of data at scale.",                        # harness, 2.4%
        "The app shipped in dark mode last quarter.",                 # mode, 1.6%
        "Documents printed in black and white.",                      # black, 1.1%
        "A ray of sunshine in the office.",                           # ray, 1.4%
        "Our chef prepares lunch daily.",                             # chef, 1.0%
        "No puppet strings attached to this role.",                   # puppet, 1.4%
        "The vault of our building is secure.",                       # vault, 0.9%
        "The office is a busy hive of activity.",                     # hive, 0.8%
        "Fiber optic cabling experience required.",                   # fiber, 1.1%
        "Temporal reasoning about event ordering.",                   # temporal, 1.1%
        "You will segment our customer base.",                        # segment, 0.6%
        "Claude Monet painted here in 1875.",                         # claude, 4.5%
        "Stripe the walls with fresh paint.",                         # stripe, 1.2%
        "A robot arm on the assembly line.",                          # robot, 0.9%
    ],
)
def test_an_ambiguous_name_needs_product_context(sentence: str) -> None:
    """None of these mention a technology, so none may produce one.

    A wrong skill is worse than a missing one: it silently corrupts every query
    that filters on the column, whereas a miss is one line to fix.
    """
    assert extract(sentence) == []


@pytest.mark.parametrize(
    "sentence,expected",
    [
        ("Build system uses a Makefile and CMake.", "make"),
        ("Styling with Sass/Less and PostCSS.", "less"),
        ("SwiftUI and Combine framework experience.", "combine"),
        ("HashiCorp Vault for secrets management.", "vault"),
        ("Apache Hive and Presto for querying.", "hive"),
        ("Ray Serve for model deployment.", "ray"),
        ("Ansible, Chef and Puppet for configuration management.", "chef"),
        ("Temporal.io workflows in production.", "temporal"),
        ("The Claude API and Anthropic models.", "claude"),
        ("Stripe API for payments processing.", "stripe"),
        ("Robot Framework for acceptance tests.", "robot framework"),
        ("kind clusters for local Kubernetes development.", "kind"),
        ("Room database on Android.", "room"),
        ("Harness.io for continuous delivery.", "harness"),
        ("Mode Analytics dashboards for the team.", "mode"),
        ("Foundation CSS framework experience.", "foundation css"),
        ("Go Fiber framework for the API.", "fiber"),
        ("Twilio Segment for event tracking.", "segment"),
    ],
)
def test_the_qualified_patterns_still_find_the_product(
    sentence: str, expected: str
) -> None:
    """Qualification trades recall for precision; it must not trade away all of it."""
    assert expected in extract(sentence)


# --- the expansion itself ---------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Spring Boot microservices with Hibernate.", {"spring boot", "hibernate"}),
        ("FastAPI and Pydantic on the backend.", {"fastapi", "pydantic"}),
        ("Deploy with ArgoCD and Kustomize.", {"argocd", "kustomize"}),
        ("Snowflake, dbt and Airflow for the warehouse.",
         {"snowflake", "dbt", "airflow"}),
        ("LangChain and pgvector for retrieval.", {"langchain", "pgvector"}),
        ("Terraform modules deployed to EKS on AWS.",
         {"terraform", "eks", "aws"}),
        ("OpenTelemetry traces shipped to Grafana Tempo.",
         {"opentelemetry", "tempo", "grafana"}),
        ("Playwright and Vitest for the test suite.", {"playwright", "vitest"}),
        ("OAuth 2.0 and OIDC with Keycloak.", {"oauth", "oidc", "keycloak"}),
        ("Kotlin with Jetpack Compose and Retrofit.",
         {"kotlin", "jetpack compose", "retrofit"}),
    ],
)
def test_the_new_terms_are_found(text: str, expected: set[str]) -> None:
    assert expected <= set(extract(text))


def test_the_vocabulary_grew_without_losing_anything() -> None:
    """The expansion is additive by construction.

    These were all in the 133-term vocabulary and each is verified individually
    elsewhere; this is a cheap guard that a bulk edit did not drop a section.
    """
    for name in ("python", "java", "javascript", "typescript", "go", "rust",
                 "c++", "c#", "kubernetes", "docker", "terraform", "aws",
                 "postgres", "kafka", "spark", "pytorch", "excel", "salesforce"):
        assert name in VOCABULARY, f"{name} disappeared from the vocabulary"
    assert len(VOCABULARY) > 700
