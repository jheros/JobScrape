"""The incremental-refresh fast path.

A repeat sweep skips the detail request for postings already stored. That is
what takes a full sweep from ~3 hours to ~25 minutes, but it is only safe if a
shallow re-observation can never destroy data captured on the first pass.

These tests guard that invariant, because the failure mode is silent: the run
would report success while quietly emptying every description in the archive.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobscrape import db
from jobscrape.models import Job
from jobscrape.sources.ashby import Ashby
from jobscrape.sources.base import PRESERVED_ON_REFRESH, shallow_refresh


class CountingFetcher:
    """Serves payloads and counts detail requests so skipping is observable."""

    def __init__(self, listing: Any, detail: Any) -> None:
        self._listing = listing
        self._detail = detail
        self.detail_calls = 0

    async def post_json(self, url: str, body: Any, **kw: Any) -> Any:
        if "ApiJobPosting" in url:
            self.detail_calls += 1
            return {"data": {"jobPosting": self._detail}}
        return {"data": {"jobBoard": self._listing}}

    async def get_json(self, url: str, **kw: Any) -> Any:
        return None

    async def get_text(self, url: str, **kw: Any) -> Any:
        return None


LISTING = {
    "teams": [{"id": "t1", "name": "Engineering"}],
    "jobPostings": [
        {"id": "job-1", "title": "Backend Engineer", "locationName": "Austin, TX",
         "employmentType": "FullTime", "teamId": "t1"},
        {"id": "job-2", "title": "Platform Engineer", "locationName": "Remote - US",
         "employmentType": "FullTime", "teamId": "t1"},
    ],
}
DETAIL = {
    "id": "job-1", "title": "Backend Engineer",
    "descriptionHtml": "<p>Build the backend. Salary $150,000 - $190,000.</p>",
    "publishedDate": "2026-06-01", "departmentName": "Engineering",
    "workplaceType": "Remote",
}


# --- the shallow_refresh contract ----------------------------------------

def test_shallow_refresh_clears_only_detail_fields() -> None:
    job = Job(
        source="ashby", source_job_id="1", title="SWE", company="Acme",
        job_url="https://x/1", location_raw="Austin, TX", department="Engineering",
        employment_type="FullTime",
        description_html="<p>Long description</p>",
        posted_at="2026-06-01", salary_min=100.0, salary_max=200.0,
        salary_currency="USD",
    )
    shallow_refresh(job)
    # Detail-only fields are dropped so the upsert preserves the stored values.
    assert job.description_html is None
    assert job.description_text is None
    assert job.posted_at is None
    assert job.salary_min is None and job.salary_max is None
    # Listing-derived fields survive, so genuine edits are still picked up.
    assert job.title == "SWE"
    assert job.location_raw == "Austin, TX"
    assert job.department == "Engineering"
    assert job.employment_type == "full_time"   # normalized from Ashby's "FullTime"


def test_preserved_set_matches_the_upsert() -> None:
    """The two lists must agree or a shallow pass destroys data.

    If a field is cleared here but *not* COALESCEd by `upsert_jobs`, the NULL is
    written straight over the stored value.
    """
    import inspect

    source = inspect.getsource(db.upsert_jobs)
    # `raw` maps to the raw_json column via Job.to_row().
    for field in PRESERVED_ON_REFRESH - {"raw"}:
        assert f'"{field}"' in source, (
            f"{field} is cleared by shallow_refresh but not preserved by upsert_jobs"
        )
    assert '"raw_json"' in source


# --- the adapter actually skips ------------------------------------------

async def test_known_postings_skip_their_detail_request() -> None:
    fetcher = CountingFetcher(LISTING, DETAIL)
    await Ashby().fetch_board(fetcher, "acme", known_ids={"job-1", "job-2"})
    assert fetcher.detail_calls == 0, "stored postings must cost no detail request"


async def test_only_new_postings_cost_a_request() -> None:
    fetcher = CountingFetcher(LISTING, DETAIL)
    jobs = await Ashby().fetch_board(fetcher, "acme", known_ids={"job-1"})
    assert fetcher.detail_calls == 1, "exactly the one unknown posting"
    assert len(jobs) == 2, "known postings are still reported as still-listed"


async def test_first_sweep_fetches_everything() -> None:
    fetcher = CountingFetcher(LISTING, DETAIL)
    await Ashby().fetch_board(fetcher, "acme", known_ids=set())
    assert fetcher.detail_calls == 2


async def test_known_posting_is_emitted_shallow() -> None:
    """It must still appear, or closure detection would mark it closed."""
    fetcher = CountingFetcher(LISTING, DETAIL)
    jobs = await Ashby().fetch_board(fetcher, "acme", known_ids={"job-1"})
    known = next(j for j in jobs if j.source_job_id == "job-1")
    assert known.description_html is None      # not re-fetched
    assert known.title == "Backend Engineer"   # listing data retained
    assert known.job_url.startswith("https://jobs.ashbyhq.com/acme/job-1")


# --- end to end: the data must survive a second sweep --------------------

async def test_second_sweep_preserves_everything(tmp_path) -> None:
    """The whole point: re-running must not empty the archive.

    First sweep stores full detail; second sweep skips the detail calls; every
    stored field must be byte-identical afterwards.
    """
    path = tmp_path / "inc.db"

    first = CountingFetcher(LISTING, DETAIL)
    jobs = await Ashby().fetch_board(first, "acme", known_ids=set())
    with db.session(path) as conn:
        db.upsert_jobs(conn, jobs)
        before = dict(conn.execute(
            "SELECT description_text, posted_at, salary_min, salary_max, salary_currency,"
            " last_seen_at FROM jobs WHERE source_job_id = 'job-1'"
        ).fetchone())

    assert before["description_text"], "first sweep must capture the description"
    assert before["posted_at"], "first sweep must capture the date"

    # Second sweep: everything is known, so no detail requests at all.
    with db.session(path) as conn:
        known = db.known_job_ids(conn, "ashby", "acme")
        assert known == {"job-1", "job-2"}

        second = CountingFetcher(LISTING, DETAIL)
        again = await Ashby().fetch_board(second, "acme", known_ids=known)
        assert second.detail_calls == 0

        db.upsert_jobs(conn, again)
        after = dict(conn.execute(
            "SELECT description_text, posted_at, salary_min, salary_max, salary_currency,"
            " last_seen_at FROM jobs WHERE source_job_id = 'job-1'"
        ).fetchone())

    for field in ("description_text", "posted_at", "salary_min", "salary_max",
                  "salary_currency"):
        assert after[field] == before[field], f"{field} was damaged by the shallow pass"
    assert after["last_seen_at"] >= before["last_seen_at"], "still-listed must be recorded"


async def test_known_ids_are_scoped_to_one_board(tmp_path) -> None:
    """Ids must be looked up per board, or one company's ids would suppress
    detail fetches for another's."""
    path = tmp_path / "scope.db"
    with db.session(path) as conn:
        db.upsert_jobs(conn, [
            Job(source="ashby", source_job_id="job-1", title="SWE", company="Acme",
                company_slug="acme", job_url="https://x/1"),
            Job(source="ashby", source_job_id="job-9", title="SWE", company="Globex",
                company_slug="globex", job_url="https://x/9"),
        ])
        assert db.known_job_ids(conn, "ashby", "acme") == {"job-1"}
        assert db.known_job_ids(conn, "ashby", "globex") == {"job-9"}
        assert db.known_job_ids(conn, "lever", "acme") == set()


# --- remembering rejected postings ---------------------------------------

def test_rejected_postings_are_not_refetched(tmp_path) -> None:
    """The other half of the saving.

    Postings whose detail we downloaded and then dropped were being re-fetched
    on every sweep forever — measured at 158 of 312 on real Ashby boards.
    """
    with db.session(tmp_path / "r.db") as conn:
        db.upsert_jobs(conn, [
            Job(source="ashby", source_job_id="kept-1", title="SWE", company="Acme",
                company_slug="acme", job_url="https://x/1"),
        ])
        # Two rejected: one on role/region (no date), one outside the window.
        db.record_rejected(conn, "ashby", "acme", [
            ("rej-role", None),
            ("rej-old", "2020-01-01T00:00:00+00:00"),
        ])
        skip = db.skippable_ids(conn, "ashby", "acme",
                                since="2026-05-01", until="2026-07-29")
        assert skip == {"kept-1", "rej-role", "rej-old"}


def test_widening_the_window_reconsiders_a_dated_rejection(tmp_path) -> None:
    """A date-based rejection must not become permanent.

    If the window later includes the posting, it has to be looked at again
    rather than silently skipped forever.
    """
    with db.session(tmp_path / "r.db") as conn:
        db.record_rejected(conn, "ashby", "acme", [("old", "2024-03-01T00:00:00+00:00")])

        narrow = db.skippable_ids(conn, "ashby", "acme",
                                  since="2026-01-01", until="2026-12-31")
        assert "old" in narrow, "outside the window: still skippable"

        wide = db.skippable_ids(conn, "ashby", "acme",
                                since="2024-01-01", until="2026-12-31")
        assert "old" not in wide, "now in range: must be re-evaluated"


def test_role_rejections_stay_skipped_whatever_the_window(tmp_path) -> None:
    """Role and region come from listing data, so the verdict cannot change."""
    with db.session(tmp_path / "r.db") as conn:
        db.record_rejected(conn, "ashby", "acme", [("nope", None)])
        for since in ("2020-01-01", "2026-07-01", None):
            assert "nope" in db.skippable_ids(conn, "ashby", "acme", since=since)


def test_rejected_records_are_board_scoped(tmp_path) -> None:
    with db.session(tmp_path / "r.db") as conn:
        db.record_rejected(conn, "ashby", "acme", [("a", None)])
        assert db.skippable_ids(conn, "ashby", "globex") == set()


# --- board_health: backoff and half-open recovery ---------------------------

def test_backoff_only_starts_after_three_consecutive_failures() -> None:
    """One transient timeout must cost nothing — a board is only backed off
    once it has failed repeatedly."""
    from jobscrape import db

    with db.session() as conn:
        db.add_companies(conn, "greenhouse", ["flaky"])
        for expected in (1, 2):
            db.mark_board_failure(conn, "greenhouse", "flaky")
            row = conn.execute(
                "SELECT consecutive_failures, cooldown_until FROM companies "
                "WHERE ats='greenhouse' AND slug='flaky'"
            ).fetchone()
            assert row["consecutive_failures"] == expected
            assert row["cooldown_until"] is None, "no cooldown below 3 failures"

        db.mark_board_failure(conn, "greenhouse", "flaky")
        row = conn.execute(
            "SELECT consecutive_failures, cooldown_until FROM companies "
            "WHERE ats='greenhouse' AND slug='flaky'"
        ).fetchone()
        assert row["consecutive_failures"] == 3
        assert row["cooldown_until"] is not None
        assert db.boards_in_cooldown(conn, "greenhouse") == {"flaky"}


def test_one_success_self_heals_a_board() -> None:
    from jobscrape import db

    with db.session() as conn:
        db.add_companies(conn, "greenhouse", ["flaky2"])
        for _ in range(4):
            db.mark_board_failure(conn, "greenhouse", "flaky2")
        assert "flaky2" in db.boards_in_cooldown(conn, "greenhouse")

        db.mark_board_success(conn, "greenhouse", "flaky2")
        assert db.boards_in_cooldown(conn, "greenhouse") == set()


def test_one_recovery_releases_every_board_of_that_platform() -> None:
    """A platform-wide outage backs off each board independently, and each
    would otherwise serve out its own timer long after the outage ended."""
    from jobscrape import db

    with db.session() as conn:
        slugs = [f"co{i}" for i in range(5)]
        db.add_companies(conn, "ashby", slugs)
        for slug in slugs:
            for _ in range(3):
                db.mark_board_failure(conn, "ashby", slug)
        assert db.boards_in_cooldown(conn, "ashby") == set(slugs)

        cleared = db.clear_ats_cooldowns(conn, "ashby")
        assert cleared == len(slugs)
        assert db.boards_in_cooldown(conn, "ashby") == set()


def test_clearing_one_platform_leaves_another_alone() -> None:
    from jobscrape import db

    with db.session() as conn:
        db.add_companies(conn, "ashby", ["a1"])
        db.add_companies(conn, "lever", ["l1"])
        for _ in range(3):
            db.mark_board_failure(conn, "ashby", "a1")
            db.mark_board_failure(conn, "lever", "l1")

        db.clear_ats_cooldowns(conn, "ashby")
        assert db.boards_in_cooldown(conn, "ashby") == set()
        assert db.boards_in_cooldown(conn, "lever") == {"l1"}


# --- derived columns must survive a shallow refresh ------------------------
#
# `skills`, `evergreen_text` and `scam_signals` are all computed at upsert time
# from `description_text`. That column is COALESCE-preserved, but the derived
# ones are assigned directly — so without an explicit guard, the second sweep of
# every board recomputes them from a NULL description and stores the empty
# result over good data. The damage compounds silently and looks like "the
# extractor is broken" rather than "the upsert overwrote it".


def _stored(conn: db.Connection, job_id: str) -> dict[str, Any]:
    return conn.execute(
        "SELECT skills, evergreen_text, scam_signals, description_text, seniority "
        "FROM jobs WHERE source = ? AND source_job_id = ?",
        ("greenhouse", job_id),
    ).fetchone()


def test_shallow_refresh_does_not_erase_skills(_isolated_db) -> None:
    """The decisive test for the whole feature.

    A repeat sweep sends exactly this: the same posting, with the description
    stripped by `shallow_refresh`. If `skills` is not guarded, it comes back
    empty and every already-known posting loses its skills on run two.
    """
    conn = _isolated_db
    full = Job(
        source="greenhouse", source_job_id="j1", title="Senior Backend Engineer",
        company="Acme", job_url="https://x/j1",
        description_text="We use Python, Kubernetes and PostgreSQL on AWS.",
    )
    db.upsert_jobs(conn, [full])
    first = _stored(conn, "j1")
    assert "python" in first["skills"]
    assert "kubernetes" in first["skills"]

    # Second sweep: the posting is already known, so the adapter emits a
    # shallow row with no description.
    db.upsert_jobs(conn, [shallow_refresh(
        Job(source="greenhouse", source_job_id="j1", title="Senior Backend Engineer",
            company="Acme", job_url="https://x/j1",
            description_text="We use Python, Kubernetes and PostgreSQL on AWS.")
    )])
    second = _stored(conn, "j1")
    assert second["skills"] == first["skills"], (
        "a shallow refresh erased the skills — the CASE WHEN guard in "
        "upsert_jobs is missing or wrong"
    )
    assert second["description_text"] == first["description_text"]


def test_a_real_description_update_does_replace_the_skills(_isolated_db) -> None:
    """The guard must not freeze the column: when a description genuinely
    changes, the skills have to follow it."""
    conn = _isolated_db
    db.upsert_jobs(conn, [Job(
        source="greenhouse", source_job_id="j2", title="Engineer", company="Acme",
        job_url="https://x/j2", description_text="Python and Django shop.")])
    assert "django" in _stored(conn, "j2")["skills"]

    db.upsert_jobs(conn, [Job(
        source="greenhouse", source_job_id="j2", title="Engineer", company="Acme",
        job_url="https://x/j2", description_text="We have moved to Go and Kubernetes.")])
    updated = _stored(conn, "j2")["skills"]
    assert "kubernetes" in updated
    assert "django" not in updated


def test_seniority_is_refreshed_from_the_title_not_preserved(_isolated_db) -> None:
    """The asymmetry with skills, made explicit.

    Seniority derives from the title, which a shallow refresh keeps — so it is
    assigned unconditionally and correctly tracks a retitled posting.
    """
    conn = _isolated_db
    db.upsert_jobs(conn, [Job(
        source="greenhouse", source_job_id="j3", title="Software Engineer",
        company="Acme", job_url="https://x/j3", description_text="Python.")])
    assert _stored(conn, "j3")["seniority"] == "unknown"

    # The employer retitles the req; a shallow pass still carries the new title.
    db.upsert_jobs(conn, [shallow_refresh(Job(
        source="greenhouse", source_job_id="j3", title="Staff Software Engineer",
        company="Acme", job_url="https://x/j3", description_text="Python."))])
    row = _stored(conn, "j3")
    assert row["seniority"] == "staff", "seniority must track the title"
    assert "python" in row["skills"], "and skills must still be preserved"
