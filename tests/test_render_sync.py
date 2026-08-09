"""Tests for the pure mapping functions in render_sync, plus `sync()` itself
against a real `conn` (the shared test Postgres database) and a fake
`render_conn` that just records what it was asked to do — Render's own
schema doesn't exist in this test environment, but `sync()` never reads
anything back from it, so recording calls is enough to verify which rows it
decided to upsert versus delete.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError

from jobscrape import db, dedupe
from jobscrape.models import Job
from jobscrape import render_sync
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
    a result back from `render_conn`, so recording is enough.

    ``fail_deletes_for`` simulates a DELETE hitting Render's own
    `reviews.job_id` foreign key -- a dedup_key in that set raises
    `IntegrityError` (and must be followed by a `rollback()` before this
    fake conn accepts anything else, same as a real Postgres connection
    after an aborted statement)."""

    def __init__(self, fail_deletes_for: set = frozenset()) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.rollback_calls = 0
        self.commit_calls = 0
        self._fail_deletes_for = fail_deletes_for
        self._aborted = False

    def execute(self, sql: str, params: tuple = ()) -> None:
        if self._aborted:
            raise AssertionError("executed on a connection still mid-aborted-transaction")
        self.calls.append((sql, params))
        if sql.strip().startswith("DELETE") and any(
            k in self._fail_deletes_for for k in params
        ):
            self._aborted = True
            raise IntegrityError("DELETE FROM jobs WHERE dedup_key = ?", params, Exception("fk"))

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1
        self._aborted = False


class _FakeRenderKeysConn:
    """`backfill_local_sync_status` only ever asks Render one thing: which
    dedup_keys currently exist there. No upserts/deletes to record."""

    def __init__(self, dedup_keys: list[str]) -> None:
        self._keys = dedup_keys

    def execute(self, sql: str, params: tuple = ()) -> list[dict[str, str]]:
        assert sql.strip().startswith("SELECT") and "dedup_key" in sql
        return [{"dedup_key": k} for k in self._keys]


def _upserted_keys(calls: list[tuple[str, tuple]]) -> list[str]:
    """Every dedup_key upserted across all INSERT calls, whether a given
    call is the single-row `_UPSERT_SQL` (dedup_key at index 2) or a
    batched multi-row `_batch_upsert_sql` (params flattened 15-per-row,
    dedup_key at index 2 of each chunk) -- `sync()` picks whichever shape
    based on how many rows were pending at flush time, so a test asserting
    on upserted keys needs to handle both."""
    keys = []
    for sql, params in calls:
        if not sql.strip().startswith("INSERT"):
            continue
        for i in range(0, len(params), 15):
            keys.append(params[i + 2])
    return keys


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


def test_sync_skips_a_delete_blocked_by_a_dependent_review() -> None:
    """Render's `reviews.job_id` foreign key can block a hard DELETE for a
    row a site visitor has reviewed -- real production behavior, not
    something jobscrape's own schema can predict or manage. One
    un-deletable row must not abort every other delete/upsert queued behind
    it in the same batch, and the connection must be usable again
    afterward (a real Postgres connection needs an explicit ROLLBACK after
    an aborted statement before it will accept anything else)."""
    with db.session() as conn:
        db.upsert_jobs(conn, [
            _dup_job(source_job_id="q0", posted_at="2026-07-01"),
            _dup_job(source_job_id="q1", posted_at="2026-07-02"),
            _dup_job(source_job_id="q2", posted_at="2026-07-03",
                     title="Backend Engineer -Remote"),
        ])
        result = dedupe.resolve_all(conn)
        assert result["collapsed"] == 1

        blocked_key = _dedup_key("adzuna", "q0", "https://x/q0",
                                  "iOS Engineer -Remote", "Quora")
        render = _FakeRenderConn(fail_deletes_for={blocked_key})
        stats = sync(conn, render)

        assert stats["skipped"] == 1
        assert stats["deleted"] == 0  # q0 was the only closed/duplicate row
        assert render.rollback_calls == 1
        # q2 (an ordinary canonical row, queued after q0 in changed_at order)
        # must still have reached an upsert -- the abort must not have taken
        # the rest of the batch down with it.
        assert _dedup_key("adzuna", "q2", "https://x/q0",
                           "Backend Engineer -Remote", "Quora") \
            in _upserted_keys(render.calls)


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


# --- sync(): batches writes and commits incrementally -----------------------


def test_sync_batches_upserts_and_commits_incrementally(monkeypatch) -> None:
    """Round-trip latency to Render, not query cost, is what made a large
    sync take hours (see `_BATCH_SIZE`'s docstring) -- three rows through a
    batch size of 2 must produce one 2-row batched INSERT plus one 1-row
    INSERT (not three separate single-row calls), and must commit after
    each flush rather than once at the very end, so a crash mid-run only
    loses the batch still in flight."""
    monkeypatch.setattr(render_sync, "_BATCH_SIZE", 2)
    with db.session() as conn:
        db.upsert_jobs(conn, [
            _dup_job(source_job_id="b0", posted_at="2026-07-01"),
            _dup_job(source_job_id="b1", posted_at="2026-07-02"),
            _dup_job(source_job_id="b2", posted_at="2026-07-03"),
        ])

        render = _FakeRenderConn()
        stats = sync(conn, render)

        assert stats["upserted"] == 3
        upserts = [c for c in render.calls if c[0].strip().startswith("INSERT")]
        assert len(upserts) == 2
        assert len(upserts[0][1]) == 2 * 15  # first flush: the full batch of 2
        assert len(upserts[1][1]) == 1 * 15  # second flush: the trailing 1
        assert set(_upserted_keys(render.calls)) == {
            _dedup_key("adzuna", "b0", "https://x/q0", "iOS Engineer -Remote", "Quora"),
            _dedup_key("adzuna", "b1", "https://x/q0", "iOS Engineer -Remote", "Quora"),
            _dedup_key("adzuna", "b2", "https://x/q0", "iOS Engineer -Remote", "Quora"),
        }
        # Two flushes -> two commits, not one commit for the whole pass.
        assert render.commit_calls == 2


def test_sync_batch_delete_falls_back_to_one_row_when_one_is_blocked() -> None:
    """A batched multi-row DELETE aborts entirely if ANY key in it hits
    Render's `reviews.job_id` foreign key -- Postgres aborts the whole
    statement, not just the offending row. `sync()` must retry that batch
    one row at a time so only the truly blocked row is skipped, not the
    other closed/duplicate rows that happened to share its batch."""
    with db.session() as conn:
        db.upsert_jobs(conn, [
            _dup_job(source_job_id="d0", posted_at="2026-07-01"),
            _dup_job(source_job_id="d1", posted_at="2026-07-02",
                     title="Backend Engineer -Remote"),
        ])
        for sid in ("d0", "d1"):
            conn.execute(
                "UPDATE jobs SET closed_at = now() WHERE source_job_id = ?", (sid,)
            )
        conn.commit()

        blocked_key = _dedup_key("adzuna", "d0", "https://x/q0",
                                  "iOS Engineer -Remote", "Quora")
        render = _FakeRenderConn(fail_deletes_for={blocked_key})
        stats = sync(conn, render)

        assert stats["skipped"] == 1
        assert stats["deleted"] == 1  # d1 still got deleted despite d0 blocking the batch
        # One rollback for the failed 2-row batch, one more for d0's own
        # failed single-row retry in the fallback.
        assert render.rollback_calls == 2


# --- sync(): stamps local render_synced_at/render_sync_status ---------------


def _local_sync_state(conn, source_job_id: str) -> tuple:
    row = conn.execute(
        "SELECT render_synced_at, render_sync_status FROM jobs WHERE source_job_id = ?",
        (source_job_id,),
    ).fetchone()
    return row["render_synced_at"], row["render_sync_status"]


def test_sync_marks_local_rows_synced_after_a_successful_batch_upsert(monkeypatch) -> None:
    """The Jobs dashboard needs to know whether a row actually reached
    Render, not just render_sync's own run-level watermark -- a successful
    batch flush must stamp every row in it, in the same batch (one extra
    local UPDATE, not one per row)."""
    monkeypatch.setattr(render_sync, "_BATCH_SIZE", 2)
    with db.session() as conn:
        db.upsert_jobs(conn, [
            _dup_job(source_job_id="s0", posted_at="2026-07-01"),
            _dup_job(source_job_id="s1", posted_at="2026-07-02"),
            _dup_job(source_job_id="s2", posted_at="2026-07-03"),
        ])

        render = _FakeRenderConn()
        stats = sync(conn, render)
        assert stats["upserted"] == 3

        for sid in ("s0", "s1", "s2"):
            synced_at, status = _local_sync_state(conn, sid)
            assert synced_at is not None
            assert status == "synced"


def test_sync_marks_a_blocked_row_distinctly_from_pending() -> None:
    """A row Render's `reviews.job_id` foreign key blocked must read as
    'blocked' locally, not indistinguishable from a row never attempted."""
    with db.session() as conn:
        db.upsert_jobs(conn, [
            _dup_job(source_job_id="k0", posted_at="2026-07-01"),
            _dup_job(source_job_id="k1", posted_at="2026-07-02",
                     title="Backend Engineer -Remote"),
        ])
        for sid in ("k0", "k1"):
            conn.execute(
                "UPDATE jobs SET closed_at = now() WHERE source_job_id = ?", (sid,)
            )
        conn.commit()

        blocked_key = _dedup_key("adzuna", "k0", "https://x/q0",
                                  "iOS Engineer -Remote", "Quora")
        render = _FakeRenderConn(fail_deletes_for={blocked_key})
        stats = sync(conn, render)
        assert stats["skipped"] == 1 and stats["deleted"] == 1

        k0_synced_at, k0_status = _local_sync_state(conn, "k0")
        k1_synced_at, k1_status = _local_sync_state(conn, "k1")
        assert k0_status == "blocked" and k0_synced_at is not None
        assert k1_status == "synced" and k1_synced_at is not None
        deletes = [c for c in render.calls if c[0].strip().startswith("DELETE")]
        # The failed 2-row batch, then the fallback's two single-row retries.
        assert len(deletes) == 3


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

        assert stats["upserted"] == 3
        keys = set(_upserted_keys(render.calls))
        assert keys == {
            _dedup_key("adzuna", "t0", "https://x/q0", "iOS Engineer -Remote", "Quora"),
            _dedup_key("adzuna", "t1", "https://x/q0", "iOS Engineer -Remote", "Quora"),
            _dedup_key("adzuna", "t2", "https://x/q0", "iOS Engineer -Remote", "Quora"),
        }


# --- backfill_local_sync_status(): catching up rows synced before the local
# bookkeeping columns existed -------------------------------------------------


def test_backfill_stamps_a_row_already_present_in_render() -> None:
    with db.session() as conn:
        db.upsert_jobs(conn, [_dup_job(source_job_id="b0", posted_at="2026-07-01")])
        render = _FakeRenderKeysConn([_dedup_key("adzuna", "b0", "https://x/q0",
                                                   "iOS Engineer -Remote", "Quora")])
        result = render_sync.backfill_local_sync_status(conn, render)

        assert result == {"candidates": 1, "matched": 1, "stamped": 1, "dry_run": False}
        synced_at, status = _local_sync_state(conn, "b0")
        assert synced_at is not None and status == "synced"


def test_backfill_leaves_a_row_not_found_in_render_alone() -> None:
    with db.session() as conn:
        db.upsert_jobs(conn, [_dup_job(source_job_id="b1", posted_at="2026-07-01")])
        render = _FakeRenderKeysConn([])  # Render has nothing at all
        result = render_sync.backfill_local_sync_status(conn, render)

        assert result == {"candidates": 1, "matched": 0, "stamped": 0, "dry_run": False}
        synced_at, status = _local_sync_state(conn, "b1")
        assert synced_at is None and status is None


def test_backfill_excludes_a_closed_row_even_if_still_in_render() -> None:
    """A closed row that's still sitting in Render needs sync()'s own DELETE
    path, not to be marked 'synced' -- that would hide the still-pending
    removal."""
    with db.session() as conn:
        db.upsert_jobs(conn, [_dup_job(source_job_id="b2", posted_at="2026-07-01")])
        conn.execute("UPDATE jobs SET closed_at = now() WHERE source_job_id = 'b2'")
        conn.commit()
        render = _FakeRenderKeysConn([_dedup_key("adzuna", "b2", "https://x/q0",
                                                   "iOS Engineer -Remote", "Quora")])
        result = render_sync.backfill_local_sync_status(conn, render)

        assert result["candidates"] == 0
        synced_at, status = _local_sync_state(conn, "b2")
        assert synced_at is None and status is None


def test_backfill_excludes_a_duplicate_row_even_if_still_in_render() -> None:
    with db.session() as conn:
        db.upsert_jobs(conn, [
            _dup_job(source_job_id="b3", posted_at="2026-07-01"),
            _dup_job(source_job_id="b4", posted_at="2026-07-02"),
        ])
        result = dedupe.resolve_all(conn)
        assert result["collapsed"] == 1

        keys = [
            _dedup_key("adzuna", "b3", "https://x/q0", "iOS Engineer -Remote", "Quora"),
            _dedup_key("adzuna", "b4", "https://x/q0", "iOS Engineer -Remote", "Quora"),
        ]
        render = _FakeRenderKeysConn(keys)
        backfill_result = render_sync.backfill_local_sync_status(conn, render)

        # Only the canonical survivor is a candidate; the suppressed dupe isn't.
        assert backfill_result["candidates"] == 1
        assert backfill_result["stamped"] == 1


def test_backfill_dry_run_reports_without_writing() -> None:
    with db.session() as conn:
        db.upsert_jobs(conn, [_dup_job(source_job_id="b5", posted_at="2026-07-01")])
        render = _FakeRenderKeysConn([_dedup_key("adzuna", "b5", "https://x/q0",
                                                   "iOS Engineer -Remote", "Quora")])
        result = render_sync.backfill_local_sync_status(conn, render, dry_run=True)

        assert result == {"candidates": 1, "matched": 1, "stamped": 0, "dry_run": True}
        synced_at, status = _local_sync_state(conn, "b5")
        assert synced_at is None and status is None


def test_backfill_batches_across_more_rows_than_one_batch_size(monkeypatch) -> None:
    monkeypatch.setattr(render_sync, "_BATCH_SIZE", 2)
    with db.session() as conn:
        db.upsert_jobs(conn, [
            _dup_job(source_job_id=f"c{i}", posted_at="2026-07-01") for i in range(5)
        ])
        keys = [_dedup_key("adzuna", f"c{i}", "https://x/q0",
                            "iOS Engineer -Remote", "Quora") for i in range(5)]
        render = _FakeRenderKeysConn(keys)
        result = render_sync.backfill_local_sync_status(conn, render)

        assert result == {"candidates": 5, "matched": 5, "stamped": 5, "dry_run": False}
        for i in range(5):
            synced_at, status = _local_sync_state(conn, f"c{i}")
            assert synced_at is not None and status == "synced"
