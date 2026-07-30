"""Identity hashes used to recognise the same role posted more than once.

Each test here targets one documented subtlety adapted from freehire's
`rolefingerprint.go` — see `jobscrape/fingerprint.py`'s module docstring for
the reasoning behind each one.
"""

from __future__ import annotations

from jobscrape.fingerprint import (
    content_hash,
    normalize_text,
    role_fingerprint,
    role_key,
    strip_trailing_clause,
)
from jobscrape.models import Job


def _job(**kw) -> Job:
    base = dict(
        source="greenhouse", source_job_id="1", title="Backend Engineer",
        company="Acme", company_slug="acme", job_url="https://x/1",
    )
    base.update(kw)
    return Job(**base)  # type: ignore[arg-type]


# --- normalize_text ---------------------------------------------------------

def test_tags_become_spaces_not_nothing() -> None:
    """`a</p><p>b` must not normalise to `ab` — the boundary matters."""
    assert normalize_text("a</p><p>b") == "a b"
    assert "ab" not in normalize_text("a</p><p>b")


def test_entities_decode_after_tags_are_stripped() -> None:
    """A literal '&lt;div&gt;' in visible text must never be read as markup."""
    assert normalize_text("use &lt;div&gt; here") == "use <div> here"


def test_normalize_is_case_and_whitespace_insensitive() -> None:
    assert normalize_text("  Build   Things  ") == "build things"


def test_normalize_empty_input() -> None:
    assert normalize_text(None) == ""
    assert normalize_text("") == ""


# --- strip_trailing_clause ---------------------------------------------------

def test_strips_one_trailing_location_clause() -> None:
    assert strip_trailing_clause("Senior Engineer, Krakow") == "Senior Engineer"
    assert strip_trailing_clause("Senior Engineer, Berlin") == "Senior Engineer"


def test_dash_and_pipe_need_a_leading_space() -> None:
    """In-word hyphens (front-end) must survive; only a genuine ' - ' clause splits."""
    assert strip_trailing_clause("Front-End Engineer") == "Front-End Engineer"
    assert strip_trailing_clause("Backend Engineer - Remote") == "Backend Engineer"
    assert strip_trailing_clause("Growth Designer | Marketing") == "Growth Designer"


def test_two_word_guard_leaves_title_unchanged() -> None:
    """Stripping must never reduce a title below two words.

    Otherwise a generic single token becomes the cluster key and merges
    unrelated roles at the same company.
    """
    assert strip_trailing_clause("Engineer - Payments") == "Engineer - Payments"
    assert strip_trailing_clause("Staff Engineer") == "Staff Engineer"


def test_strip_trailing_clause_empty_input() -> None:
    assert strip_trailing_clause("") == ""
    assert strip_trailing_clause(None) == ""


# --- role_fingerprint / role_key --------------------------------------------

def test_markup_only_differences_collapse() -> None:
    """The same job, one plain-text copy and one HTML copy, must fingerprint equal."""
    plain = _job(description_text="Build the payments pipeline.")
    html = _job(description_html="<p>Build the payments pipeline.</p>")
    assert role_fingerprint(plain) == role_fingerprint(html)


def test_location_clause_variants_share_a_fingerprint() -> None:
    krakow = _job(title="Senior Engineer, Krakow", description_text="Same role.")
    berlin = _job(title="Senior Engineer, Berlin", description_text="Same role.")
    assert role_fingerprint(krakow) == role_fingerprint(berlin)


def test_distinct_descriptions_do_not_collapse() -> None:
    """Two real, different requisitions at the same company must stay distinct.

    This is the guard measured against the live archive: a title-only key
    would wrongly merge hundreds of genuinely different postings.
    """
    a = _job(source_job_id="a", description_text="Own the payments platform.")
    b = _job(source_job_id="b", description_text="Own the identity platform.")
    assert role_fingerprint(a) != role_fingerprint(b)
    # role_key is looser (no description) and DOES consider these the same
    # role — that's by design; only role_fingerprint discriminates on content.
    assert role_key(a) == role_key(b)


def test_different_companies_never_collapse() -> None:
    a = _job(company="Acme", company_slug="acme", description_text="Build things.")
    b = _job(company="Globex", company_slug="globex", description_text="Build things.")
    assert role_fingerprint(a) != role_fingerprint(b)
    assert role_key(a) != role_key(b)


def test_fingerprint_excludes_posted_at_and_url() -> None:
    """A repost differs from the original in exactly the date and URL —
    excluding them is what lets the fingerprint recognise a repost at all."""
    original = _job(posted_at="2026-01-01", job_url="https://x/1", description_text="Same text.")
    repost = _job(posted_at="2026-07-01", job_url="https://x/2", description_text="Same text.")
    assert role_fingerprint(original) == role_fingerprint(repost)


def test_blank_company_slug_yields_no_fingerprint() -> None:
    """A row that never resolved a company slug must not cluster with anything —
    an empty leading key component would degenerate to matching across every
    company at once."""
    job = Job(source="adzuna", source_job_id="1", title="Engineer", company="",
              job_url="https://x/1", description_text="Text.")
    assert job.company_slug is None
    assert role_fingerprint(job) == ""
    assert role_key(job) == ""


def test_blank_title_yields_no_fingerprint() -> None:
    job = _job(title="")
    assert role_fingerprint(job) == ""
    assert role_key(job) == ""


# --- content_hash ------------------------------------------------------------

def test_content_hash_includes_posted_at_unlike_role_fingerprint() -> None:
    """content_hash is the CHANGE signal and must react to a date refresh —
    the opposite of role_fingerprint. Never use it to cluster reposts."""
    a = _job(posted_at="2026-01-01", description_text="Same text.")
    b = _job(posted_at="2026-07-01", description_text="Same text.")
    assert content_hash(a) != content_hash(b)
    assert role_fingerprint(a) == role_fingerprint(b)


def test_content_hash_changes_when_description_edited() -> None:
    a = _job(description_text="Original description.")
    b = _job(description_text="Edited description.")
    assert content_hash(a) != content_hash(b)


# --- unwrap_parens (cross-source key only) ---------------------------------

def test_parenthesised_qualifier_merges_into_the_same_role_key() -> None:
    """freehire measured that counting these as two roles inflated their
    cross-source absent-count by 13%."""
    from jobscrape.fingerprint import unwrap_parens

    assert role_key(_job(title="Data Engineer (Semi Senior)")) == \
           role_key(_job(title="Data Engineer Semi Senior"))
    assert unwrap_parens("Data Engineer (Semi Senior)") == "Data Engineer  Semi Senior "


def test_paren_contents_are_kept_not_dropped() -> None:
    """Dropping the contents would collapse two unrelated roles into one."""
    assert role_key(_job(title="Go Developer (Remote)")) != \
           role_key(_job(title="Rust Developer (Remote)"))


def test_unwrap_parens_composes_with_the_clause_strip() -> None:
    """A parenthesised gender marker plus a city suffix still collapses."""
    assert role_key(_job(title="Senior Engineer (m/w/d), Krakau")) == \
           role_key(_job(title="Senior Engineer (m/w/d), Wien"))


def test_role_fingerprint_does_not_unwrap_parens() -> None:
    """Only the loose cross-source key unwraps; the strict same-source
    fingerprint stays literal so it never over-merges within one board."""
    a = _job(title="Data Engineer (Semi Senior)", description_text="Same body.")
    b = _job(title="Data Engineer Semi Senior", description_text="Same body.")
    assert role_fingerprint(a) != role_fingerprint(b)
