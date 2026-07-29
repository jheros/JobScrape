"""The archive behavior that makes period coverage possible over time.

Because the APIs only expose currently-open postings, complete coverage of a
date range depends on the archive accumulating across runs and never losing
detail it has already captured.
"""

from __future__ import annotations

from jobscrape import db
from jobscrape.models import Job


def _job(job_id: str, **kw: object) -> Job:
    base = dict(
        source="greenhouse", source_job_id=job_id, title="Software Engineer",
        company="Acme", job_url=f"https://example.com/{job_id}",
        location_raw="Austin, TX", posted_at="2026-06-15",
    )
    base.update(kw)
    return Job(**base)  # type: ignore[arg-type]


def test_closed_postings_are_retained(tmp_path) -> None:
    """A job that disappears from the API stays in the archive.

    This is the whole point: yesterday's run is the only record that a
    now-closed posting ever existed.
    """
    path = tmp_path / "a.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [_job("1"), _job("2"), _job("3")])
        # Next day the board only still lists job 1.
        db.upsert_jobs(conn, [_job("1")])
        assert db.stats(conn)["total_jobs"] == 3


def test_last_seen_tracks_liveness(tmp_path) -> None:
    """`last_seen_at` is how you tell a live posting from a closed one."""
    path = tmp_path / "a.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [_job("1"), _job("2")])
        first = conn.execute(
            "SELECT last_seen_at FROM jobs WHERE source_job_id='2'"
        ).fetchone()["last_seen_at"]
        db.upsert_jobs(conn, [_job("1", title="Senior Software Engineer")])
        rows = {
            r["source_job_id"]: r
            for r in conn.execute("SELECT source_job_id, last_seen_at, title FROM jobs")
        }
        assert rows["1"]["last_seen_at"] >= first
        assert rows["2"]["last_seen_at"] == first  # untouched
        assert rows["1"]["title"] == "Senior Software Engineer"


def test_enrichment_never_regresses(tmp_path) -> None:
    """A shallow pass must not blank fields a deep pass already filled."""
    path = tmp_path / "a.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [
            _job("1", description_html="<p>Full posting text here</p>",
                 salary_min=120_000, salary_max=180_000, salary_currency="USD"),
        ])
        # A later listing-only pass carries no description or salary.
        db.upsert_jobs(conn, [_job("1", description_html=None, salary_min=None)])
        row = conn.execute(
            "SELECT description_text, salary_min, salary_currency FROM jobs"
        ).fetchone()
        assert row["description_text"] == "Full posting text here"
        assert row["salary_min"] == 120_000
        assert row["salary_currency"] == "USD"


def test_date_range_query_spans_accumulated_runs(tmp_path) -> None:
    path = tmp_path / "a.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [_job("may", posted_at="2026-05-10")])
        db.upsert_jobs(conn, [_job("jun", posted_at="2026-06-10")])
        db.upsert_jobs(conn, [_job("jul", posted_at="2026-07-10")])
        window = db.query_jobs(conn, since="2026-06-01", until="2026-06-30")
        assert [r["source_job_id"] for r in window] == ["jun"]
        assert len(db.query_jobs(conn, since="2026-05-01", until="2026-07-31")) == 3


def test_stats_reports_range_and_sources(tmp_path) -> None:
    path = tmp_path / "a.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [
            _job("1", posted_at="2026-05-10", state="TX"),
            Job(source="lever", source_job_id="2", title="Backend Engineer",
                company="Globex", job_url="https://x/2", posted_at="2026-07-20",
                state="CA"),
        ])
        stats = db.stats(conn)
        assert stats["total_jobs"] == 2
        assert stats["by_source"] == {"greenhouse": 1, "lever": 1}
        assert stats["date_range"] == ["2026-05-10", "2026-07-20"]
        assert set(stats["top_states"]) == {"TX", "CA"}
