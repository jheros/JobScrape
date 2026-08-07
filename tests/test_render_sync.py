"""Tests for the pure mapping functions in render_sync, plus `sync()` itself
against a real `conn` (the shared test Postgres database) and a fake
`render_conn` that just records what it was asked to do — Render's own
schema doesn't exist in this test environment, but `sync()` never reads
anything back from it, so recording calls is enough to verify which rows it
decided to upsert versus delete.
"""

from __future__ import annotations

from typing import Any

from jobscrape import db, dedupe
from jobscrape.models import Job
from jobscrape.render_sync import _dedup_key, _skills_array, _to_render_row, sync


def test_dedup_key_prefers_external_job_id():
    assert _dedup_key("greenhouse", "123", "https://x/1", "Engineer", "Acme") \
        == "greenhouse:123"


def test_dedup_key_falls_back_to_jd_url():
    assert _dedup_key("greenhouse", None, "https://x/1", "Engineer", "Acme") \
        == "https://x/1"


def test_dedup_key_falls_back_to_source_title_company():
    assert _dedup_key("greenhouse", None, None, "Engineer", "Acme") \
        == "greenhouse:Engineer:Acme"


def test_skills_array_splits_and_strips():
    assert _skills_array("python, django , postgres") == ["python", "django", "postgres"]


def test_skills_array_drops_blank_segments():
    assert _skills_array("python,,  ,django") == ["python", "django"]


def test_skills_array_empty_or_none():
    assert _skills_array("") == []
    assert _skills_array(None) == []


def test_to_render_row_maps_every_field():
    row = {
        "source": "greenhouse", "source_job_id": "123",
        "title": "Backend Engineer", "company": "Acme",
        "location_raw": "Remote - US",
        "salary_min": 120000.0, "salary_max": 180000.6,
        "apply_url": "https://boards.greenhouse.io/acme/jobs/123",
        "job_url": "https://boards.greenhouse.io/acme/jobs/123",
        "description_text": "Build things.",
        "posted_date": "2026-07-01",
        "first_seen_at": "2026-07-01T00:00:00+00:00",
        "skills": "python,django",
        "department": "Backend Engineering",
        "seniority": "senior",
    }
    out = _to_render_row(row)
    assert out == {
        "external_job_id": "123",
        "source_platform": "greenhouse",
        "dedup_key": "greenhouse:123",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Remote - US",
        "salary_min": 120000,
        "salary_max": 180001,
        "jd_url": "https://boards.greenhouse.io/acme/jobs/123",
        "jd_text": "Build things.",
        "posted_date": "2026-07-01",
        "created_at": "2026-07-01T00:00:00+00:00",
        "skills": ["python", "django"],
        "domain": "Backend Engineering",
        "level": "senior",
    }


def test_to_render_row_falls_back_to_job_url_when_no_apply_url():
    row = {
        "source": "lever", "source_job_id": "9", "title": "Engineer",
        "company": "Beta", "location_raw": None, "salary_min": None,
        "salary_max": None, "apply_url": None,
        "job_url": "https://jobs.lever.co/beta/9",
        "description_text": None, "posted_date": None,
        "first_seen_at": None, "skills": "", "department": None,
        "seniority": None,
    }
    out = _to_render_row(row)
    assert out["jd_url"] == "https://jobs.lever.co/beta/9"
    assert out["skills"] == []
    assert out["salary_min"] is None
    assert out["salary_max"] is None


# --- sync(): duplicate suppression must reach Render as a delete ------------


class _FakeRenderConn:
    """Records every call rather than writing anywhere -- Render's own
    schema doesn't exist in this test environment, and `sync()` never reads
    a result back from `render_conn`, so recording is enough."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.calls.append((sql, params))

    def commit(self) -> None:
        pass


def _dup_job(**kw: Any) -> Job:
    base = dict(
        source="adzuna", source_job_id="q0", title="iOS Engineer -Remote",
        company="Quora", company_slug="quora", job_url="https://x/q0",
        posted_at="2026-07-01", description_text="Build iOS. " * 5,
    )
    base.update(kw)
    return Job(**base)  # type: ignore[arg-type]


def test_sync_deletes_a_row_dedupe_just_suppressed() -> None:
    """A row that becomes a duplicate must have its OWN Render copy removed --
    the old `duplicate_of IS NULL` filter hid this transition from `sync()`
    entirely, so an already-published duplicate stayed in Render forever."""
    with db.session() as conn:
        db.upsert_jobs(conn, [
            _dup_job(source_job_id="q0", posted_at="2026-07-01"),
            _dup_job(source_job_id="q1", posted_at="2026-07-02"),
        ])
        result = dedupe.resolve_all(conn)
        assert result["collapsed"] == 1

        dup = conn.execute(
            "SELECT duplicate_of FROM jobs WHERE source_job_id = 'q0'"
        ).fetchone()
        assert dup["duplicate_of"] is not None

        render = _FakeRenderConn()
        stats = sync(conn, render)

        deletes = [c for c in render.calls if c[0].strip().startswith("DELETE")]
        upserts = [c for c in render.calls if c[0].strip().startswith("INSERT")]
        assert stats["deleted"] == 1 and stats["upserted"] == 1
        # q0's own dedup_key, not q1's -- q0 is the suppressed one.
        assert deletes and deletes[0][1] == (_dedup_key("adzuna", "q0", "https://x/q0",
                                                          "iOS Engineer -Remote", "Quora"),)
        assert upserts  # q1, the canonical, still gets published normally


def test_sync_republishes_a_row_after_clear_duplicate() -> None:
    """Reversing a mis-cluster must reach Render as a fresh upsert, not stay
    excluded as if it were still suppressed."""
    with db.session() as conn:
        db.upsert_jobs(conn, [
            _dup_job(source_job_id="q0", posted_at="2026-07-01"),
            _dup_job(source_job_id="q1", posted_at="2026-07-02"),
        ])
        result = dedupe.resolve_all(conn)
        assert result["collapsed"] == 1

        q0 = conn.execute(
            "SELECT id FROM jobs WHERE source_job_id = 'q0'"
        ).fetchone()
        db.clear_duplicate(conn, q0["id"])

        render = _FakeRenderConn()
        stats = sync(conn, render)

        # Both rows are canonical again by the time this pass runs, so both
        # are upserts -- none of q0's own dedup_key appears in a DELETE.
        assert stats["deleted"] == 0 and stats["upserted"] == 2
        deletes = [c for c in render.calls if c[0].strip().startswith("DELETE")]
        assert not deletes


# --- sync(): a `--limit` cut must never split a tied `changed_at` group -----


def test_sync_limit_does_not_permanently_strand_a_tied_row() -> None:
    """`upsert_jobs` stamps ONE `last_seen_at` across every row in a batch,
    so several rows sharing the exact same `changed_at` is the common case,
    not a rare edge. A `--limit`-capped run that cuts inside that tie and
    advances the watermark to the boundary value would permanently strand
    the sibling it left behind: the next run's comparison is strict
    (`changed_at > ?`), so a row tied with the new watermark would never be
    selected by any future run, capped or not."""
    with db.session() as conn:
        # All three land in one `upsert_jobs` call, so all three share one
        # `last_seen_at` -- a genuine three-way tie, not three close values.
        db.upsert_jobs(conn, [
            _dup_job(source_job_id="t0", posted_at="2026-07-01"),
            _dup_job(source_job_id="t1", posted_at="2026-07-02"),
            _dup_job(source_job_id="t2", posted_at="2026-07-03"),
        ])

        render = _FakeRenderConn()
        # A limit smaller than the tie group: with no spillover, this would
        # process only 2 of the 3 and advance the watermark past all three's
        # shared timestamp, stranding the third permanently.
        stats = sync(conn, render, limit=2)

        upserts = [c for c in render.calls if c[0].strip().startswith("INSERT")]
        assert stats["upserted"] == 3 and len(upserts) == 3
        keys = {c[1][2] for c in upserts}  # dedup_key is the 3rd bound param
        assert keys == {
            _dedup_key("adzuna", "t0", "https://x/q0", "iOS Engineer -Remote", "Quora"),
            _dedup_key("adzuna", "t1", "https://x/q0", "iOS Engineer -Remote", "Quora"),
            _dedup_key("adzuna", "t2", "https://x/q0", "iOS Engineer -Remote", "Quora"),
        }
