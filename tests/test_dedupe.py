"""Repost / cross-source duplicate collapse — see `jobscrape/dedupe.py`.

The scenarios here mirror real clusters found by running this against the
live 44,436-row archive: the Quora same-source repost pattern (one role
posted per US city), the Coinbase over-merge guard (two genuinely distinct
"Senior Software Engineer" reqs that a title-only key would wrongly collapse
— 680 such rows were measured on the real archive), and the chain-compression
bug that measurement surfaced (a tier-1 canonical later reassigned by tier 2
must not leave its tier-1 dependents pointing at a non-canonical row).
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from jobscrape import config, db, dedupe
from jobscrape.models import Job


def _job(**kw) -> Job:
    base = dict(
        source="greenhouse", source_job_id="1", title="Backend Engineer",
        company="Acme", company_slug="acme", job_url="https://x/1",
        posted_at="2026-07-01",
    )
    base.update(kw)
    return Job(**base)  # type: ignore[arg-type]


# --- compute_clusters: pure logic, no database ------------------------------

def test_same_source_repost_collapses_to_most_recent() -> None:
    rows = [
        {"id": 1, "source": "adzuna", "company_slug": "quora", "posted_at": "2026-07-01",
         "first_seen_at": "2026-07-01", "role_fingerprint": "fp1", "role_key": "rk1"},
        {"id": 2, "source": "adzuna", "company_slug": "quora", "posted_at": "2026-07-05",
         "first_seen_at": "2026-07-05", "role_fingerprint": "fp1", "role_key": "rk1"},
        {"id": 3, "source": "adzuna", "company_slug": "quora", "posted_at": "2026-07-03",
         "first_seen_at": "2026-07-03", "role_fingerprint": "fp1", "role_key": "rk1"},
    ]
    assignments, counts = dedupe.compute_clusters(rows)
    assert assignments == {1: 2, 3: 2}
    assert counts["tier1_clusters"] == 1 and counts["tier1_rows"] == 2
    assert counts["tier2_clusters"] == 0


def test_distinct_ats_requisitions_never_collapse() -> None:
    """The Coinbase guard: two real reqs sharing role_key must stay separate."""
    rows = [
        {"id": 1, "source": "greenhouse", "company_slug": "coinbase", "posted_at": "2026-07-01",
         "first_seen_at": "2026-07-01", "role_fingerprint": "fp-req-a", "role_key": "rk-swe"},
        {"id": 2, "source": "greenhouse", "company_slug": "coinbase", "posted_at": "2026-07-02",
         "first_seen_at": "2026-07-02", "role_fingerprint": "fp-req-b", "role_key": "rk-swe"},
    ]
    assignments, counts = dedupe.compute_clusters(rows)
    assert assignments == {}
    assert counts["tier1_clusters"] == 0 and counts["tier2_clusters"] == 0


def test_cross_source_collapses_aggregator_into_ats() -> None:
    rows = [
        {"id": 1, "source": "greenhouse", "company_slug": "coinbase", "posted_at": "2026-07-01",
         "first_seen_at": "2026-07-01", "role_fingerprint": "fp-gh", "role_key": "rk-staff"},
        {"id": 2, "source": "adzuna", "company_slug": "coinbase", "posted_at": "2026-07-10",
         "first_seen_at": "2026-07-10", "role_fingerprint": "fp-agg", "role_key": "rk-staff"},
    ]
    assignments, counts = dedupe.compute_clusters(rows)
    # The aggregator row (2) must point at the ATS row (1), even though the
    # aggregator row is more recent — direction is a safety property here,
    # not a tiebreak.
    assert assignments == {2: 1}
    assert counts["tier2_clusters"] == 1 and counts["tier2_rows"] == 1


def test_two_ats_rows_never_merge_via_tier2() -> None:
    """Tier 2 may only ever suppress an aggregator copy — never merge two
    direct ATS rows into each other, even if they share a role_key."""
    rows = [
        {"id": 1, "source": "greenhouse", "company_slug": "acme", "posted_at": "2026-07-01",
         "first_seen_at": "2026-07-01", "role_fingerprint": "fp-a", "role_key": "rk-shared"},
        {"id": 2, "source": "lever", "company_slug": "acme", "posted_at": "2026-07-02",
         "first_seen_at": "2026-07-02", "role_fingerprint": "fp-b", "role_key": "rk-shared"},
    ]
    assignments, _ = dedupe.compute_clusters(rows)
    assert assignments == {}


def test_chain_compression_no_duplicate_points_at_a_duplicate() -> None:
    """Regression test for a real bug found validating against the live archive.

    Several identical-content aggregator rows (tier 1 collapses them to one
    survivor) plus one ATS row (tier 2 then reassigns that survivor) must all
    end up pointing DIRECTLY at the ATS row — none of them may point at the
    tier-1 survivor once IT has itself become a duplicate.
    """
    rows = [
        {"id": 1, "source": "adzuna", "company_slug": "foocorp", "posted_at": "2026-07-01",
         "first_seen_at": "2026-07-01", "role_fingerprint": "fp-dup", "role_key": "rk-shared"},
        {"id": 2, "source": "adzuna", "company_slug": "foocorp", "posted_at": "2026-07-02",
         "first_seen_at": "2026-07-02", "role_fingerprint": "fp-dup", "role_key": "rk-shared"},
        {"id": 3, "source": "adzuna", "company_slug": "foocorp", "posted_at": "2026-07-05",
         "first_seen_at": "2026-07-05", "role_fingerprint": "fp-dup", "role_key": "rk-shared"},
        {"id": 4, "source": "greenhouse", "company_slug": "foocorp", "posted_at": "2026-07-10",
         "first_seen_at": "2026-07-10", "role_fingerprint": "fp-gh", "role_key": "rk-shared"},
    ]
    assignments, counts = dedupe.compute_clusters(rows)
    # Rows 1 and 2 are tier-1 dependents of row 3 (the most recent adzuna
    # copy); row 3 is then itself reassigned by tier 2 to row 4. All three
    # must resolve directly to 4 — none may point at 3.
    assert assignments == {1: 4, 2: 4, 3: 4}
    assert counts["tier1_rows"] == 2
    assert counts["tier2_rows"] == 1
    # No entry in the assignments dict may itself be a key that's ALSO a
    # target of another entry — i.e. no two-hop chains survive.
    targets = set(assignments.values())
    assert not (targets & set(assignments.keys()))


def test_avature_counts_as_a_direct_board_not_an_aggregator() -> None:
    """`restricted_board_sources()` (Avature) is opt-in and imported lazily,
    but its rows are a genuine per-company board, same trust level as
    Greenhouse or Lever once collected -- excluding it from tier 2's
    "known board" check read an Avature row as aggregator-provenance and let
    it be silently suppressed by another board's canonical, exactly the
    outcome tier 2's "never merge two ATS requisitions into each other"
    guarantee exists to prevent."""
    rows = [
        {"id": 1, "source": "greenhouse", "company_slug": "acme", "posted_at": "2026-07-01",
         "first_seen_at": "2026-07-01", "role_fingerprint": "fp-gh", "role_key": "rk-shared"},
        {"id": 2, "source": "avature", "company_slug": "acme", "posted_at": "2026-07-02",
         "first_seen_at": "2026-07-02", "role_fingerprint": "fp-av", "role_key": "rk-shared"},
    ]
    assignments, counts = dedupe.compute_clusters(rows)
    # Two DIRECT boards sharing a role_key must never merge via tier 2 --
    # same invariant `test_two_ats_rows_never_merge_via_tier2` proves for
    # greenhouse/lever.
    assert assignments == {}
    assert counts["tier2_clusters"] == 0


def test_avature_can_still_be_the_tier2_canonical_for_a_real_aggregator() -> None:
    """The other half: an aggregator copy of an Avature-listed role must
    still collapse into the Avature row, the same as it would for any other
    real ATS board."""
    rows = [
        {"id": 1, "source": "avature", "company_slug": "acme", "posted_at": "2026-07-01",
         "first_seen_at": "2026-07-01", "role_fingerprint": "fp-av", "role_key": "rk-shared"},
        {"id": 2, "source": "adzuna", "company_slug": "acme", "posted_at": "2026-07-10",
         "first_seen_at": "2026-07-10", "role_fingerprint": "fp-agg", "role_key": "rk-shared"},
    ]
    assignments, counts = dedupe.compute_clusters(rows)
    assert assignments == {2: 1}
    assert counts["tier2_clusters"] == 1


def test_rows_without_a_fingerprint_never_cluster() -> None:
    rows = [
        {"id": 1, "source": "adzuna", "company_slug": "", "posted_at": "2026-07-01",
         "first_seen_at": "2026-07-01", "role_fingerprint": "", "role_key": ""},
        {"id": 2, "source": "adzuna", "company_slug": "", "posted_at": "2026-07-02",
         "first_seen_at": "2026-07-02", "role_fingerprint": "", "role_key": ""},
    ]
    assignments, _ = dedupe.compute_clusters(rows)
    assert assignments == {}


# --- integration: real Postgres writes --------------------------------------

def test_resolve_all_writes_duplicate_of_and_preserves_triage() -> None:
    with db.session() as conn:
        jobs = [
            _job(source="adzuna", source_job_id="q0", company="Quora", title="iOS Engineer -Remote",
                 posted_at="2026-07-01", description_text="Build iOS. " * 5),
            _job(source="adzuna", source_job_id="q1", company="Quora", title="iOS Engineer -Remote",
                 posted_at="2026-07-02", description_text="Build iOS. " * 5),
        ]
        db.set_job_status(conn, "adzuna", "q0", "saved", notes="looks interesting")
        db.upsert_jobs(conn, jobs)

        result = dedupe.resolve_all(conn)
        assert result["collapsed"] == 1

        q0 = conn.execute(
            "SELECT id, duplicate_of, duplicate_marked_at FROM jobs WHERE source_job_id = 'q0'"
        ).fetchone()
        assert q0["duplicate_of"] is not None
        # `render_sync` reads this to notice a row just became suppressed —
        # without it, a row already synced to Render before this clustering
        # ran would never get its stale Render copy deleted.
        assert q0["duplicate_marked_at"] is not None

        status = conn.execute(
            "SELECT status, notes FROM job_status WHERE source_job_id = 'q0'"
        ).fetchone()
        assert status["status"] == "saved" and status["notes"] == "looks interesting"

        db.clear_duplicate(conn, q0["id"])
        restored = conn.execute(
            "SELECT duplicate_of, duplicate_marked_at FROM jobs WHERE source_job_id = 'q0'"
        ).fetchone()
        assert restored["duplicate_of"] is None
        # Bumped again on the reversal too -- `render_sync` needs to notice
        # this transition just as much as the original suppression, so the
        # row gets re-published instead of staying excluded.
        assert restored["duplicate_marked_at"] is not None


def test_resolve_all_dry_run_writes_nothing() -> None:
    with db.session() as conn:
        jobs = [
            _job(source="adzuna", source_job_id="d0", company="DryRunCo",
                 title="Engineer -Remote", posted_at="2026-07-01",
                 description_text="Same text. " * 5),
            _job(source="adzuna", source_job_id="d1", company="DryRunCo",
                 title="Engineer -Remote", posted_at="2026-07-02",
                 description_text="Same text. " * 5),
        ]
        db.upsert_jobs(conn, jobs)
        result = dedupe.resolve_all(conn, dry_run=True)
        assert result["collapsed"] == 1
        still_none = conn.execute(
            "SELECT COUNT(*) c FROM jobs WHERE source = 'adzuna' AND company = 'DryRunCo' "
            "AND duplicate_of IS NOT NULL"
        ).fetchone()["c"]
        assert still_none == 0


def test_resolve_scoped_only_touches_one_company() -> None:
    with db.session() as conn:
        db.upsert_jobs(conn, [
            _job(source="adzuna", source_job_id="s0", company="ScopedCo", company_slug="scopedco",
                 title="Engineer -Remote", posted_at="2026-07-01", description_text="Text. " * 5),
            _job(source="adzuna", source_job_id="s1", company="ScopedCo", company_slug="scopedco",
                 title="Engineer -Remote", posted_at="2026-07-02", description_text="Text. " * 5),
            _job(source="adzuna", source_job_id="o0", company="OtherCo", company_slug="otherco",
                 title="Engineer -Remote", posted_at="2026-07-01", description_text="Text. " * 5),
            _job(source="adzuna", source_job_id="o1", company="OtherCo", company_slug="otherco",
                 title="Engineer -Remote", posted_at="2026-07-02", description_text="Text. " * 5),
        ])
        result = dedupe.resolve_scoped(conn, "scopedco")
        assert result["collapsed"] == 1
        other_untouched = conn.execute(
            "SELECT COUNT(*) c FROM jobs WHERE company_slug = 'otherco' AND duplicate_of IS NOT NULL"
        ).fetchone()["c"]
        assert other_untouched == 0


# --- advisory lock: cmd_dedupe's cross-process backfill serialization ------

def test_advisory_lock_blocks_a_second_backfill_until_the_first_releases() -> None:
    """`cmd_dedupe` (cli.py) wraps `resolve_all` in
    `pg_advisory_xact_lock(hashtext('jobscrape:dedupe')::bigint)` so two
    subprocesses' full-archive backfills never race. This exercises the
    actual lock against Postgres — no mocking — mirroring the two-connection
    check done manually before this was wired into `cmd_dedupe`.

    Uses its own engine rather than `db.connect()`: the `_isolated_db`
    autouse fixture monkeypatches that to always hand back the *same*
    connection for the test's duration (so writes roll back at teardown),
    which would make both "connections" here the same Postgres backend —
    advisory locks are reentrant within one backend, so the test would
    pass for the wrong reason. Two real connections need two real sessions.
    """
    lock_sql = text("SELECT pg_try_advisory_xact_lock(hashtext('jobscrape:dedupe')::bigint) AS ok")
    engine = create_engine(config.TEST_DATABASE_URL, poolclass=NullPool, future=True)
    conn1 = engine.connect()
    conn2 = engine.connect()
    try:
        assert conn1.execute(lock_sql).mappings().one()["ok"] is True
        assert conn2.execute(lock_sql).mappings().one()["ok"] is False
        conn1.rollback()
        assert conn2.execute(lock_sql).mappings().one()["ok"] is True
        conn2.rollback()
    finally:
        conn1.close()
        conn2.close()
        engine.dispose()
