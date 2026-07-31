"""Purging stored postings that no longer pass the role filter.

The only destructive operation in the project, so the tests are mostly about
what it must *not* damage: triage notes, surviving duplicates, and rows that
still pass.

The orphaned-duplicate case is the one that matters most. `duplicate_of` points
at a row id and every listing filters on `duplicate_of IS NULL`, so deleting a
canonical while its duplicates survive makes those duplicates invisible
everywhere with nothing to indicate it happened. Measured on the real archive,
one purge stranded 35 rows exactly this way.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobscrape import db
from jobscrape.models import Job


def _job(job_id: str, title: str, **kw: Any) -> Job:
    return Job(
        source="greenhouse", source_job_id=job_id, title=title,
        company="Acme", company_slug="acme",
        job_url=f"https://x/{job_id}", **kw,
    )


def _ids(conn: db.Connection) -> set[str]:
    return {r["source_job_id"] for r in conn.execute("SELECT source_job_id FROM jobs")}


# --- delete_jobs ------------------------------------------------------------


def test_delete_removes_only_the_named_rows(_isolated_db) -> None:
    conn = _isolated_db
    db.upsert_jobs(conn, [
        _job("keep", "Senior Software Engineer"),
        _job("qa", "QA Engineer"),
        _job("mgr", "Engineering Manager"),
    ])
    deleted, _ = db.delete_jobs(conn, [("greenhouse", "qa"), ("greenhouse", "mgr")])
    assert deleted == 2
    assert _ids(conn) == {"keep"}


def test_delete_is_a_no_op_on_an_empty_list(_isolated_db) -> None:
    conn = _isolated_db
    db.upsert_jobs(conn, [_job("a", "Software Engineer")])
    assert db.delete_jobs(conn, []) == (0, 0)
    assert _ids(conn) == {"a"}


def test_delete_batches_past_the_chunk_size(_isolated_db) -> None:
    """The delete chunks at 500; a purge of 5,000 rows must not lose the tail."""
    conn = _isolated_db
    jobs = [_job(f"j{i}", "Software Engineer") for i in range(1200)]
    db.upsert_jobs(conn, jobs)
    deleted, _ = db.delete_jobs(conn, [("greenhouse", f"j{i}") for i in range(1200)])
    assert deleted == 1200
    assert _ids(conn) == set()


def test_triage_notes_survive_a_purge(_isolated_db) -> None:
    """`job_status` is separate precisely so a re-fetch cannot erase where you
    applied. A purge must respect the same boundary."""
    conn = _isolated_db
    db.upsert_jobs(conn, [_job("qa", "QA Engineer")])
    db.set_job_status(conn, "greenhouse", "qa", "applied", notes="spoke to the recruiter")
    db.delete_jobs(conn, [("greenhouse", "qa")])
    row = conn.execute(
        "SELECT status, notes FROM job_status WHERE source = ? AND source_job_id = ?",
        ("greenhouse", "qa"),
    ).fetchone()
    assert row is not None and row["status"] == "applied"


# --- orphaned duplicates ----------------------------------------------------


def test_a_purged_canonical_releases_its_surviving_duplicates(_isolated_db) -> None:
    """The 35-row case. Without the release these rows are suppressed forever
    by a canonical that no longer exists."""
    conn = _isolated_db
    db.upsert_jobs(conn, [
        _job("canon", "Data Engineer - Senior Manager"),
        _job("dup", "Data Engineer - Senior Associate"),
    ])
    canon_id = conn.execute(
        "SELECT id FROM jobs WHERE source_job_id = ?", ("canon",)
    ).fetchone()["id"]
    conn.execute("UPDATE jobs SET duplicate_of = ? WHERE source_job_id = ?",
                 (canon_id, "dup"))
    conn.commit()

    deleted, released = db.delete_jobs(conn, [("greenhouse", "canon")])
    assert (deleted, released) == (1, 1)
    # `jobs.duplicate_of` has a foreign key, so getting this wrong is not a
    # silent dangling pointer — the DELETE raises and the whole purge dies.
    assert conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE duplicate_of IS NOT NULL "
        "AND duplicate_of NOT IN (SELECT id FROM jobs)"
    ).fetchone()["n"] == 0
    row = conn.execute(
        "SELECT duplicate_of FROM jobs WHERE source_job_id = ?", ("dup",)
    ).fetchone()
    assert row["duplicate_of"] is None, "the survivor must become visible again"


def test_a_surviving_canonical_keeps_its_duplicates_suppressed(_isolated_db) -> None:
    """Only links pointing at a *deleted* row may be cleared."""
    conn = _isolated_db
    db.upsert_jobs(conn, [
        _job("canon", "Software Engineer"),
        _job("dup", "Software Engineer"),
    ])
    canon_id = conn.execute(
        "SELECT id FROM jobs WHERE source_job_id = ?", ("canon",)
    ).fetchone()["id"]
    conn.execute("UPDATE jobs SET duplicate_of = ? WHERE source_job_id = ?",
                 (canon_id, "dup"))
    conn.commit()

    # Deleting an unrelated row must not touch this link.
    db.upsert_jobs(conn, [_job("other", "QA Engineer")])
    _, released = db.delete_jobs(conn, [("greenhouse", "other")])
    assert released == 0
    row = conn.execute(
        "SELECT duplicate_of FROM jobs WHERE source_job_id = ?", ("dup",)
    ).fetchone()
    assert row["duplicate_of"] == canon_id


# --- the command ------------------------------------------------------------


def _args(**kw: Any):
    import argparse

    defaults = {"db": None, "dry_run": False, "sample": 0,
                "include_qa": False, "include_management": False}
    return argparse.Namespace(**{**defaults, **kw})


def test_purge_removes_qa_and_management_and_keeps_the_rest(_isolated_db) -> None:
    from jobscrape.cli import cmd_purge

    conn = _isolated_db
    db.upsert_jobs(conn, [
        _job("a", "Senior Software Engineer"),
        _job("b", "Staff Backend Engineer"),
        _job("c", "Platform Engineer"),       # named discipline, must survive
        _job("d", "Tech Lead, Payments"),     # IC, must survive
        _job("hw", "Chief Engineer"),         # senior IC, but not software
        _job("qa", "QA Engineer"),
        _job("test", "Test Engineer"),
        _job("mgr", "Engineering Manager"),
        _job("dir", "Director of Engineering"),
    ])
    assert cmd_purge(_args()) == 0
    assert _ids(conn) == {"a", "b", "c", "d"}


def test_purge_dry_run_deletes_nothing(_isolated_db) -> None:
    from jobscrape.cli import cmd_purge

    conn = _isolated_db
    db.upsert_jobs(conn, [_job("qa", "QA Engineer"), _job("a", "Software Engineer")])
    assert cmd_purge(_args(dry_run=True)) == 0
    assert _ids(conn) == {"qa", "a"}


def test_purge_records_removals_so_they_are_not_re_fetched(_isolated_db) -> None:
    """Otherwise the next sweep downloads each one again just to reject it."""
    from jobscrape.cli import cmd_purge

    conn = _isolated_db
    db.upsert_jobs(conn, [_job("qa", "QA Engineer", posted_at="2026-07-01")])
    cmd_purge(_args())
    row = conn.execute(
        "SELECT reason FROM posting_seen WHERE source = ? AND source_job_id = ?",
        ("greenhouse", "qa"),
    ).fetchone()
    assert row is not None and row["reason"] == "rejected"


def test_purge_flags_spare_each_category(_isolated_db) -> None:
    from jobscrape.cli import cmd_purge

    conn = _isolated_db
    db.upsert_jobs(conn, [_job("qa", "QA Engineer"), _job("mgr", "Engineering Manager")])
    cmd_purge(_args(include_qa=True))
    assert _ids(conn) == {"qa"}, "only the manager should have gone"


def test_purge_on_a_clean_archive_changes_nothing(_isolated_db) -> None:
    from jobscrape.cli import cmd_purge

    conn = _isolated_db
    db.upsert_jobs(conn, [_job("a", "Senior Software Engineer")])
    assert cmd_purge(_args()) == 0
    assert _ids(conn) == {"a"}
