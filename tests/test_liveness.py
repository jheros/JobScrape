"""Liveness probing.

A posting closes only on positive evidence of death, and the three-verdict
design is what makes that true: an `uncertain` read must neither add a strike
nor clear one. Collapsing that into a boolean is how a site having a bad
afternoon closes a catalogue.
"""

from __future__ import annotations

import pytest

from jobscrape.liveness import (
    CLOSE_THRESHOLD,
    EXPIRED,
    LIVE,
    MIN_CONTENT_CHARS,
    UNCERTAIN,
    apply_verdict,
    classify,
    is_probable,
    probe_url,
)

HEALTHY = (
    "<html><body><h1>Senior Backend Engineer</h1>"
    "<p>We are looking for an experienced engineer to join the payments team. "
    "You will design and operate services handling millions of transactions. "
    "Requirements include strong Python and distributed systems experience. "
    "We offer competitive compensation and equity.</p>"
    "<button>Apply now</button></body></html>"
)


def test_healthy_page_is_live() -> None:
    assert len(HEALTHY) >= MIN_CONTENT_CHARS
    assert classify(200, "https://x/job/1", HEALTHY).verdict == LIVE


def test_apply_button_alone_never_marks_a_page_expired() -> None:
    """Guards the obvious false positive in the other direction."""
    assert "Apply" in HEALTHY
    assert classify(200, "https://x/job/1", HEALTHY).verdict == LIVE


@pytest.mark.parametrize("status", [404, 410])
def test_gone_status_is_expired(status: int) -> None:
    probe = classify(status, "https://x/job/1", "")
    assert probe.verdict == EXPIRED and probe.reason == "http_gone"


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503, 504])
def test_unhappy_site_is_uncertain_not_expired(status: int) -> None:
    """The site is unhappy, which says nothing about the job."""
    assert classify(status, "https://x/job/1", HEALTHY).verdict == UNCERTAIN


@pytest.mark.parametrize("phrase", [
    "This job is no longer available",
    "The position has been filled",
    "We are no longer accepting applications",
    "This job listing is closed",
    "Applications have closed",
    "This position has expired",
    "Job not found",
])
def test_death_phrases_are_expired(phrase: str) -> None:
    body = f"<html><body><p>{phrase}</p>{'x' * 400}</body></html>"
    probe = classify(200, "https://x/job/1", body)
    assert probe.verdict == EXPIRED, phrase
    assert probe.reason == "expired_body"


def test_ordinary_description_words_do_not_trip_the_dictionary() -> None:
    """'closed' and 'filled' appear in real descriptions all the time."""
    body = (
        "<html><body><p>You will work on closed-loop control systems and "
        "filled-order reconciliation. This role is open and we are hiring. "
        "Our team has filled several senior positions this year and continues "
        "to grow across the payments and infrastructure organisations. "
        "You will collaborate with product and design on a closed beta, and "
        "own the service end to end from design through operation.</p>"
        "</body></html>"
    )
    assert len(body) >= MIN_CONTENT_CHARS
    assert classify(200, "https://x/job/1", body).verdict == LIVE


def test_listing_page_redirect_is_expired() -> None:
    body = f"<html><body><p>248 jobs found</p>{'x' * 400}</body></html>"
    probe = classify(200, "https://x/search", body)
    assert probe.verdict == EXPIRED and probe.reason == "listing_page"


def test_error_marker_in_final_url_is_expired() -> None:
    probe = classify(200, "https://x/careers?error=true", HEALTHY)
    assert probe.verdict == EXPIRED and probe.reason == "expired_url"


def test_empty_shell_is_expired() -> None:
    probe = classify(200, "https://x/job/1", "<html><body><nav>Home</nav></body></html>")
    assert probe.verdict == EXPIRED and probe.reason == "insufficient_content"


# --- the strike machine ------------------------------------------------------

def test_two_consecutive_expired_reads_close() -> None:
    strikes, close = apply_verdict(0, EXPIRED)
    assert (strikes, close) == (1, False), "one death signal is absorbed"
    strikes, close = apply_verdict(strikes, EXPIRED)
    assert (strikes, close) == (CLOSE_THRESHOLD, True)


def test_a_healthy_read_between_them_resets() -> None:
    strikes, _ = apply_verdict(0, EXPIRED)
    strikes, close = apply_verdict(strikes, LIVE)
    assert (strikes, close) == (0, False)
    strikes, close = apply_verdict(strikes, EXPIRED)
    assert close is False, "the counter restarted, so one strike is not enough"


def test_uncertain_neither_adds_nor_clears_a_strike() -> None:
    """THE reason there are three verdicts. A 503 is not evidence the posting
    is alive, so it must not clear an accumulated strike — and not evidence it
    is dead, so it must not add one."""
    assert apply_verdict(1, UNCERTAIN) == (1, False)
    assert apply_verdict(0, UNCERTAIN) == (0, False)


# --- unprobable URLs ---------------------------------------------------------

def test_container_urls_are_not_probed() -> None:
    """A Hacker News thread stays up whether or not the role was filled, so a
    probe can only ever answer LIVE — burning a request to learn nothing and
    clearing strikes earned from a real signal."""
    assert is_probable("https://news.ycombinator.com/item?id=123") is False
    assert is_probable("https://t.me/somechannel/42") is False
    assert is_probable("https://boards.greenhouse.io/acme/jobs/1") is True
    assert is_probable("") is False
    assert is_probable(None) is False


# --- the SSRF guard is wired in ---------------------------------------------

@pytest.mark.asyncio
async def test_a_blocked_address_is_uncertain_not_expired() -> None:
    """Refusing to look at something is not evidence about it."""

    class NeverCalled:
        async def get_text(self, *a, **k):
            raise AssertionError("must not fetch a blocked address")

    probe = await probe_url(NeverCalled(), "http://169.254.169.254/latest/meta-data/")
    assert probe.verdict == UNCERTAIN and probe.reason == "blocked"


@pytest.mark.asyncio
async def test_no_response_is_uncertain() -> None:
    class Dead:
        async def get_text(self, *a, **k):
            return None

    probe = await probe_url(Dead(), "https://example.com/job/1")
    assert probe.verdict == UNCERTAIN and probe.reason == "no_response"


# --- database integration ----------------------------------------------------

def test_reopen_clears_accumulated_strikes() -> None:
    """Without this the two-strike grace would apply only to a posting's first
    life: one transient death signal after a reopen would close it instantly."""
    from jobscrape import db
    from jobscrape.models import Job

    job = Job(source="hackernews", source_job_id="live1", title="Engineer",
              company="LiveCo", company_slug="liveco", job_url="https://x/1")
    with db.session() as conn:
        db.upsert_jobs(conn, [job])
        row = conn.execute(
            "SELECT id FROM jobs WHERE source_job_id = 'live1'").fetchone()
        db.record_liveness(conn, row["id"], strikes=2, close=True)

        closed = conn.execute(
            "SELECT closed_at, liveness_strikes FROM jobs WHERE id = ?", (row["id"],)
        ).fetchone()
        assert closed["closed_at"] is not None and closed["liveness_strikes"] == 2

        db.upsert_jobs(conn, [job])  # it came back
        reopened = conn.execute(
            "SELECT closed_at, liveness_strikes FROM jobs WHERE id = ?", (row["id"],)
        ).fetchone()
        assert reopened["closed_at"] is None
        assert reopened["liveness_strikes"] == 0


def test_candidates_refuses_an_empty_source_list() -> None:
    """An unbounded query here would probe the entire catalogue."""
    from jobscrape import db

    with db.session() as conn:
        assert db.liveness_candidates(conn, [], limit=10) == []
