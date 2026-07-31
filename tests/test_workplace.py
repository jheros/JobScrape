"""Tests for `workplace_type` classification and its storage.

This column is purely descriptive — nothing is filtered on it. Its job is to let
you separate real remote roles from office roles yourself, so the values need to
be trustworthy and `unknown` needs to stay honest rather than guessing.
"""

from __future__ import annotations

import pytest

from jobscrape import db
from jobscrape.models import Job, classify_workplace


@pytest.mark.parametrize(
    "hint,expected",
    [
        ("Remote", "remote"),
        ("remote", "remote"),
        ("Hybrid", "hybrid"),
        ("OnSite", "onsite"),
        ("On-site", "onsite"),
        ("Office", "onsite"),
        ("Telecommute", "remote"),
    ],
)
def test_structured_hint_wins(hint: str, expected: str) -> None:
    """A source's own workplace field beats free text, which is far noisier."""
    assert classify_workplace("Austin, TX", None, hint) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Remote - US", "remote"),
        ("Work from home", "remote"),
        ("Fully distributed team", "remote"),
        ("Austin, TX (Hybrid)", "hybrid"),
        ("3 days in office", "hybrid"),
        ("Remote-friendly", "hybrid"),
        ("NYC - On-site", "onsite"),
        ("In-person, Seattle", "onsite"),
        ("This role is not remote", "onsite"),
    ],
)
def test_text_classification(text: str, expected: str) -> None:
    assert classify_workplace(text) == expected


def test_hybrid_beats_remote() -> None:
    """"Hybrid remote" and "remote-friendly" are hybrid arrangements."""
    assert classify_workplace("hybrid remote") == "hybrid"
    assert classify_workplace("Remote-friendly (2 days in office)") == "hybrid"


def test_boolean_flag_used_when_no_wording() -> None:
    assert classify_workplace(None, True, None) == "remote"
    assert classify_workplace(None, False, None) == "onsite"


def test_bare_city_is_unknown_not_guessed() -> None:
    """A city with no workplace wording is genuinely unknown; say so."""
    assert classify_workplace("San Francisco, CA") == "unknown"
    assert classify_workplace("") == "unknown"
    assert classify_workplace(None) == "unknown"


def _job(**kw: object) -> Job:
    base = dict(
        source="t", source_job_id="1", title="Software Engineer",
        company="Acme", job_url="https://example.com/1",
    )
    base.update(kw)
    return Job(**base)  # type: ignore[arg-type]


def test_job_populates_workplace_type() -> None:
    assert _job(location_raw="Remote - US").workplace_type == "remote"
    assert _job(location_raw="Austin, TX (Hybrid)").workplace_type == "hybrid"
    assert _job(location_raw="Austin, TX").workplace_type == "unknown"


def test_job_hint_overrides_location_text() -> None:
    job = _job(location_raw="Austin, TX", workplace_hint="Remote")
    assert job.workplace_type == "remote"


def test_remote_classification_backfills_is_remote() -> None:
    job = _job(location_raw="Remote - US")
    assert job.is_remote is True


def test_explicit_workplace_type_is_respected() -> None:
    job = _job(location_raw="Austin, TX", workplace_type="hybrid")
    assert job.workplace_type == "hybrid"


def test_workplace_hint_is_not_stored() -> None:
    """The hint is an input to classification, not a column."""
    assert "workplace_hint" not in _job(workplace_hint="Remote").to_row()


def test_workplace_type_persists_and_is_queryable(tmp_path) -> None:
    path = tmp_path / "w.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [
            _job(source_job_id="a", location_raw="Remote - US"),
            _job(source_job_id="b", location_raw="Austin, TX (Hybrid)"),
            _job(source_job_id="c", location_raw="Austin, TX"),
        ])
        rows = {r["source_job_id"]: r["workplace_type"] for r in
                conn.execute("SELECT source_job_id, workplace_type FROM jobs")}
        assert rows == {"a": "remote", "b": "hybrid", "c": "unknown"}


def test_workplace_type_is_exported(tmp_path) -> None:
    import csv
    from jobscrape import export

    path = tmp_path / "w.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [_job(location_raw="Remote - US")])
        target = export.to_csv(db.query_jobs(conn), tmp_path / "out.csv")
    rows = list(csv.DictReader(target.open(encoding="utf-8")))
    assert rows[0]["workplace_type"] == "remote"


# --- the permissive remote sweep -------------------------------------------
#
# A deliberately loose rule: any word in REMOTE_MENTION appearing ANYWHERE in a
# posting makes it remote — but ONLY for a posting nothing else could classify.
# Chosen to maximise how many remote roles surface, with the false positives
# accepted knowingly (see REMOTE_MENTION's own comment).
#
# The tests that matter most here are the *precedence* ones. The sweep reads the
# whole description, and hybrid postings routinely say "2 days remote", so if it
# ever ran before the other tiers it would collapse hybrid and onsite into
# remote and destroy the distinction entirely.


@pytest.mark.parametrize(
    "word",
    ["remote", "work from home", "wfh", "telecommute",
     "home-based", "fully distributed", "work from anywhere", "remote-first"],
)
def test_any_remote_word_anywhere_marks_an_otherwise_unknown_posting(word: str) -> None:
    """Deep in the description, past the 1,500-char signal window."""
    job = Job(
        source="t", source_job_id="1", title="Software Engineer", company="Acme",
        job_url="https://x/1", location_raw="Austin, TX",
        description_text="Filler. " * 900 + f"We offer {word} arrangements.",
    )
    assert job.workplace_type == "remote"


def test_virtual_was_removed_from_the_sweep() -> None:
    """It used to be in the list above, and was measurably its worst member.

    Re-measured on the live archive: 211 of 3,063 remote postings rested on
    "virtual" alone, overwhelmingly "virtual interviews" — the hiring process,
    not the work arrangement. Nothing else about the rule changed.
    """
    job = Job(
        source="t", source_job_id="1", title="Software Engineer", company="Acme",
        job_url="https://x/1", location_raw="Austin, TX",
        description_text="Filler. " * 900 + "We conduct virtual interviews.",
    )
    assert job.workplace_type == "unknown"


def test_the_sweep_only_fills_blanks() -> None:
    """The whole scope of the feature, in one test.

    Each of these says something explicit AND mentions remote. The explicit
    statement must win — a hybrid role that says "2 days remote" is hybrid.
    """
    hybrid = Job(source="t", source_job_id="1", title="Engineer", company="A",
                 job_url="x", description_text="Hybrid schedule: 3 days in the "
                 "office, 2 days remote, work from home on Fridays.")
    assert hybrid.workplace_type == "hybrid"

    onsite = Job(source="t", source_job_id="2", title="Engineer", company="A",
                 job_url="x", description_text="This role is onsite in Austin. "
                 "Remote work is not offered. " + "Filler. " * 500)
    assert onsite.workplace_type == "onsite"


def test_a_native_field_beats_the_sweep() -> None:
    """Tier 1 evidence — the employer's own field — is never second-guessed."""
    job = Job(source="t", source_job_id="1", title="Engineer", company="A",
              job_url="x", workplace_hint="Hybrid",
              description_text="remote " * 200)
    assert job.workplace_type == "hybrid"


def test_a_posting_with_no_remote_mention_stays_unknown() -> None:
    """`unknown` still has to mean something."""
    job = Job(source="t", source_job_id="1", title="Software Engineer", company="A",
              job_url="x", location_raw="Austin, TX",
              description_text="Build services in Python. " * 200)
    assert job.workplace_type == "unknown"


def test_the_sweep_reads_past_the_truncated_signal_window() -> None:
    """This is what the `full_text` parameter exists for.

    The regular tiers see only the first 1,500 characters; the sweep sees the
    whole posting. Without that distinction this feature does nothing, since a
    mention inside 1,500 chars would already have been caught by `_RE_REMOTE`.
    """
    from jobscrape.models import classify_workplace

    signal = "Software Engineer Austin, TX " + "Filler. " * 100
    assert classify_workplace(signal, None, None) == "unknown"
    assert classify_workplace(signal, None, None,
                              full_text=signal + " remote friendly") == "remote"


def test_sweep_is_case_insensitive() -> None:
    job = Job(source="t", source_job_id="1", title="Engineer", company="A",
              job_url="x", description_text="Filler. " * 400 + "REMOTE optional")
    assert job.workplace_type == "remote"


# --- native workplace fields ------------------------------------------------


def test_oracle_workplace_type_becomes_the_hint() -> None:
    """The reported bug: Oracle states "Hybrid" outright and it was unread.

    Oracle puts `WorkplaceType` on the LIST response as well as the detail, so
    it costs no extra request — 91 of 198 listings across two live boards carry
    a value.
    """
    import asyncio
    from jobscrape.sources.oracle import Oracle

    listing = {
        "items": [{"requisitionList": [{
            "Id": "19247", "Title": "Principal Platform Engineer",
            "PrimaryLocation": "Norcross, GA, United States",
            "WorkplaceType": "Hybrid", "WorkplaceTypeCode": "ORA_HYBRID",
        }], "TotalJobsCount": 1}]
    }

    class FakeFetcher:
        async def get_json(self, url, **kw):
            return listing if "JobRequisitions?" in url or "findReqs" in url else None

    jobs = asyncio.run(Oracle().fetch_board(
        FakeFetcher(), "ebwg.fa.us2.oraclecloud.com/CX", with_details=False))
    assert jobs and jobs[0].workplace_type == "hybrid"


@pytest.mark.parametrize(
    "value,expected",
    [("true", True), ("false", False), ("TRUE", True), (True, True),
     (False, False), (None, None), ("", None), ("maybe", None)],
)
def test_usajobs_remote_indicator_parsing(value, expected) -> None:
    """USAJobs sends it as the string "true"/"false", not a boolean."""
    from jobscrape.sources.usajobs import _remote_indicator

    assert _remote_indicator(value) is expected
