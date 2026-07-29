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
