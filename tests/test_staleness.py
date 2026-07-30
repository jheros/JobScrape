"""Is this posting a real, current opening?

The verdict is time-dependent and therefore computed on read — every test here
passes an explicit `now`, which is also what makes the module testable without
freezing a clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jobscrape.staleness import (
    FRESH,
    LIKELY_EVERGREEN,
    STALE,
    classify,
    has_evergreen_text,
)

NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def _days_ago(n: int) -> datetime:
    return NOW - timedelta(days=n)


# --- individual signals -----------------------------------------------------

def test_recent_posting_with_no_other_signal_is_fresh() -> None:
    s = classify(posted_at=_days_ago(3), now=NOW)
    assert s.verdict == FRESH
    assert s.signals == 0
    assert s.age_days == 3


def test_old_posting_alone_is_stale_not_evergreen() -> None:
    """One signal is never enough — convergence needs two."""
    s = classify(posted_at=_days_ago(200), now=NOW)
    assert s.old is True
    assert s.signals == 1
    assert s.verdict == STALE


def test_old_plus_evergreen_text_converges() -> None:
    s = classify(
        posted_at=_days_ago(200),
        description="We are always hiring talented engineers.",
        now=NOW,
    )
    assert s.signals == 2
    assert s.verdict == LIKELY_EVERGREEN
    assert "posted 200 days ago" in s.reasons


def test_evergreen_text_alone_is_not_fresh_even_when_recent() -> None:
    """A brand-new 'talent pool' posting is still not a real opening."""
    s = classify(
        posted_at=_days_ago(1),
        description="Join our talent pool for future openings.",
        now=NOW,
    )
    assert s.verdict == STALE
    assert s.evergreen_text is True


# --- the independence guard -------------------------------------------------

def test_concurrent_spray_fires_one_signal_not_two() -> None:
    """mass_count is a subset of repost_count, so counting both raw would let
    a single concurrent spray reach the verdict on its own. The repost signal
    counts only historical churn beyond the concurrent copies."""
    s = classify(posted_at=_days_ago(3), repost_count=20, mass_count=20, now=NOW)
    assert s.mass_posted is True
    assert s.reposted is False, "20 concurrent copies is ONE phenomenon, not two"
    assert s.signals == 1
    assert s.verdict != LIKELY_EVERGREEN


def test_historical_churn_is_a_genuinely_separate_signal() -> None:
    """Closed-and-reposted-under-new-ids over time, plus concurrent copies."""
    s = classify(posted_at=_days_ago(3), repost_count=20, mass_count=5, now=NOW)
    assert s.mass_posted is True
    assert s.reposted is True, "15 historical reposts beyond the 5 concurrent ones"
    assert s.signals == 2
    assert s.verdict == LIKELY_EVERGREEN


def test_below_both_thresholds_fires_nothing() -> None:
    s = classify(posted_at=_days_ago(3), repost_count=4, mass_count=2, now=NOW)
    assert s.mass_posted is False and s.reposted is False


# --- the evergreen dictionary -----------------------------------------------

def test_dictionary_is_multi_word_so_ordinary_prose_never_trips_it() -> None:
    """A bare word would fire on half the catalogue."""
    assert has_evergreen_text("You will own our data pipeline and ETL jobs.") is False
    assert has_evergreen_text("Build a robust deployment pipeline.") is False
    assert has_evergreen_text("We have an open culture and flexible hours.") is False


def test_dictionary_catches_real_standing_adverts() -> None:
    for text in (
        "Join our talent pool",
        "This is a general application for future opportunities",
        "We are always looking for great people",
        "Submit a speculative application",
    ):
        assert has_evergreen_text(text) is True, text


def test_dictionary_is_case_insensitive() -> None:
    assert has_evergreen_text("JOIN OUR TALENT COMMUNITY") is True


# --- undated postings -------------------------------------------------------

def test_undated_posting_is_stale_not_fresh() -> None:
    """No date is not evidence of freshness — 'fresh' has to be earned."""
    s = classify(posted_at=None, now=NOW)
    assert s.age_days is None
    assert s.old is False
    assert s.verdict == STALE


def test_undated_posting_can_still_converge_on_other_signals() -> None:
    s = classify(
        posted_at=None, repost_count=20, mass_count=5,
        description="Ongoing recruitment for our talent network.", now=NOW,
    )
    assert s.verdict == LIKELY_EVERGREEN


# --- input tolerance --------------------------------------------------------

def test_accepts_an_iso_string_and_a_naive_datetime() -> None:
    """Postgres hands back aware datetimes; exports and tests hand back strings."""
    assert classify(posted_at="2026-07-27T00:00:00+00:00", now=NOW).age_days == 3
    assert classify(posted_at=datetime(2026, 7, 27), now=NOW).age_days == 3


def test_unparseable_date_is_treated_as_undated() -> None:
    assert classify(posted_at="not a date", now=NOW).age_days is None


def test_future_dated_posting_clamps_to_zero_rather_than_going_negative() -> None:
    s = classify(posted_at=NOW + timedelta(days=5), now=NOW)
    assert s.age_days == 0
    assert s.verdict == FRESH


# --- boundaries -------------------------------------------------------------

def test_thresholds_are_inclusive_at_the_named_day() -> None:
    assert classify(posted_at=_days_ago(90), now=NOW).old is True
    assert classify(posted_at=_days_ago(89), now=NOW).old is False
    assert classify(posted_at=_days_ago(14), now=NOW).verdict == FRESH
    assert classify(posted_at=_days_ago(15), now=NOW).verdict == STALE


# --- the SQL form must agree with the Python form ---------------------------
#
# The rule exists twice (filtering has to happen in the database, or pagination
# would run over an already-truncated page). Both forms are built from the same
# constants; this is the test that proves they cannot drift apart.

def test_sql_verdict_matches_python_verdict_row_for_row() -> None:
    from datetime import datetime, timedelta, timezone

    from jobscrape import db, staleness
    from jobscrape.models import Job

    now = datetime.now(timezone.utc)
    fixtures = [
        # (title, posted days ago, description) spanning every verdict
        ("Fresh Role", 2, "Build things."),
        ("Old Role", 200, "Build things."),
        ("Evergreen Role", 2, "Join our talent pool for future openings."),
        ("Old Evergreen Role", 200, "We are always hiring engineers."),
        ("Undated Role", None, "Build things."),
        ("Pipeline Role", 2, "You will own our data pipeline."),
    ]
    with db.session() as conn:
        jobs = []
        for i, (title, days, desc) in enumerate(fixtures):
            posted = (now - timedelta(days=days)).isoformat() if days is not None else None
            jobs.append(Job(
                source="greenhouse", source_job_id=f"stale{i}", title=title,
                company="StaleCo", company_slug="staleco",
                job_url=f"https://x/{i}", posted_at=posted, description_text=desc,
            ))
        db.upsert_jobs(conn, jobs)

        rows = conn.execute(
            f"SELECT j.source_job_id, j.posted_at, j.description_text,"
            f"       COALESCE(cl.repost_count, 1) rc, COALESCE(cl.mass_count, 1) mc,"
            f"       {staleness.sql_verdict()} AS sql_verdict "
            f"FROM jobs j {staleness.CLUSTER_JOIN} "
            f"WHERE j.company_slug = 'staleco'"
        ).fetchall()

        assert len(rows) == len(fixtures)
        for row in rows:
            python_verdict = staleness.classify(
                posted_at=row["posted_at"],
                repost_count=row["rc"],
                mass_count=row["mc"],
                description=row["description_text"],
                now=now,
            ).verdict
            assert python_verdict == row["sql_verdict"], (
                f"{row['source_job_id']}: python={python_verdict} "
                f"sql={row['sql_verdict']}"
            )


def test_evergreen_flag_is_stored_at_upsert_and_survives_a_shallow_refresh() -> None:
    """The flag is derived from the description, which upsert_jobs COALESCEs —
    so it must follow the same rule. A shallow refresh carries no description
    and would otherwise recompute the flag as false, wiping it off a posting
    whose description is still there, preserved, one column over."""
    from jobscrape import db
    from jobscrape.models import Job

    deep = Job(source="greenhouse", source_job_id="ever1", title="Engineer",
               company="EverCo", company_slug="everco", job_url="https://x/1",
               description_text="Join our talent pool for future openings.")
    with db.session() as conn:
        db.upsert_jobs(conn, [deep])
        row = conn.execute(
            "SELECT evergreen_text FROM jobs WHERE source_job_id = 'ever1'"
        ).fetchone()
        assert row["evergreen_text"] is True

        shallow = Job(source="greenhouse", source_job_id="ever1", title="Engineer",
                      company="EverCo", company_slug="everco", job_url="https://x/1")
        assert shallow.description_text in (None, "")
        db.upsert_jobs(conn, [shallow])
        row = conn.execute(
            "SELECT evergreen_text, description_text FROM jobs "
            "WHERE source_job_id = 'ever1'"
        ).fetchone()
        assert row["description_text"], "the description itself is preserved"
        assert row["evergreen_text"] is True, "so its derived flag must be too"
