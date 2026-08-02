"""Which boards a sweep actually reaches, and which it stops paying for.

Two mechanisms, opposite in direction, tested together because they share the
board loop and the same `companies` row.

**Reach.** `_fetch_boards` used to ask for `status='live'` and fall back to
`unchecked` only when there were *zero* live boards — all or nothing. A
partially validated platform was therefore pinned to its handful of known-live
boards forever, and no amount of re-running fetch would find the rest: measured
on the live archive, iCIMS swept 6 boards while 2,493 unchecked slugs sat beside
them, and 16,866 discovered boards across six platforms were unreachable.

**Cost.** The opposite failure was in the same branch: a platform with zero live
boards swept *every* unchecked slug, which is why each sweep spent ~10,000
requests on Paycom's 5,135 portals — healthy boards, real postings, none of them
software. `mark_board_yield` backs those off without ever calling them dead.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from jobscrape import cli, db
from jobscrape.models import Job
from jobscrape.sources.base import BoardSource


class FakeBoard(BoardSource):
    """A board platform whose per-slug output the test dictates."""

    name = "fake"
    ats = "fake"

    def __init__(self, postings: dict[str, list[Job] | None]) -> None:
        self.postings = postings
        self.asked: list[str] = []

    async def fetch_board(self, fetcher, slug, with_details=True,
                          prefilter=None, known_ids=None, **kw):
        self.asked.append(slug)
        return self.postings.get(slug, [])


def _job(slug: str, n: int = 1) -> Job:
    return Job(
        source="fake",
        source_job_id=f"{slug}-{n}",
        title="Senior Backend Engineer",
        company=slug,
        company_slug=slug,
        job_url=f"https://example.test/{slug}/{n}",
        location_raw="Austin, TX",
        description_text="Python, Go and Kubernetes.",
    )


def _args(**kw: Any) -> argparse.Namespace:
    defaults = dict(
        companies=None, unchecked=cli.UNCHECKED_PER_ATS, all_boards=False,
        concurrency=4, all_roles=False, worldwide=False, include_roles=None,
        include_qa=False, include_management=False, refetch_details=False,
    )
    return argparse.Namespace(**{**defaults, **kw})


async def _sweep(conn, source: FakeBoard, args: argparse.Namespace) -> dict[str, int]:
    totals, _funnel = await cli._fetch_boards(
        conn, None, "fake", None, None, args, boards={"fake": source}
    )
    return totals


def _register(conn, slugs_by_status: dict[str, list[str]]) -> None:
    for status, slugs in slugs_by_status.items():
        for slug in slugs:
            db.add_companies(conn, "fake", [slug])
            if status != "unchecked":
                db.mark_company(conn, "fake", slug, status, 0)


# --- reach ------------------------------------------------------------------


def test_a_partially_validated_platform_still_sweeps_its_unchecked_boards(_isolated_db):
    """The regression that hid 16,866 boards.

    One live board used to be enough to hide every unchecked one, because the
    fallback fired only on an empty live set.
    """
    conn = _isolated_db
    _register(conn, {"live": ["known"], "unchecked": ["new-a", "new-b"]})
    source = FakeBoard({"known": [_job("known")], "new-a": [_job("new-a")]})

    asyncio.run(_sweep(conn, source, _args()))

    assert set(source.asked) == {"known", "new-a", "new-b"}


def test_unchecked_zero_asks_for_live_boards_only(_isolated_db):
    """The escape hatch, for a sweep that must not spend on unproven slugs."""
    conn = _isolated_db
    _register(conn, {"live": ["known"], "unchecked": ["new-a"]})
    source = FakeBoard({"known": [_job("known")]})

    asyncio.run(_sweep(conn, source, _args(unchecked=0)))

    assert source.asked == ["known"]


def test_the_unchecked_slice_is_capped(_isolated_db):
    """Unbounded is what cost ~10,000 requests a sweep on Paycom alone."""
    conn = _isolated_db
    _register(conn, {"unchecked": [f"slug-{i}" for i in range(20)]})
    source = FakeBoard({})

    asyncio.run(_sweep(conn, source, _args(unchecked=5)))

    assert len(source.asked) == 5


def test_an_unchecked_board_that_answers_is_promoted_to_live(_isolated_db):
    """A fetch IS validate's probe with details on top, so it may record the
    verdict — otherwise the random unchecked slice would have to rediscover the
    same board by chance on every sweep."""
    conn = _isolated_db
    _register(conn, {"unchecked": ["yields", "silent"]})
    source = FakeBoard({"yields": [_job("yields")], "silent": []})

    asyncio.run(_sweep(conn, source, _args()))

    rows = {r["slug"]: r["status"] for r in db.get_companies(conn, ats="fake")}
    assert rows["yields"] == "live"
    # Ambiguous, so deliberately NOT written: `prefilter` runs inside
    # `fetch_board`, so a board of forklift-mechanic postings comes back `[]`
    # exactly like a board with nothing on it. `validate` owns empty/dead.
    assert rows["silent"] == "unchecked"


def test_random_ordering_returns_the_same_rows(_isolated_db):
    """`order="random"` changes which slice a limit takes, never the set."""
    conn = _isolated_db
    _register(conn, {"unchecked": [f"slug-{i}" for i in range(30)]})

    ordered = db.get_companies(conn, ats="fake", status="unchecked")
    shuffled = db.get_companies(conn, ats="fake", status="unchecked", order="random")

    assert {r["slug"] for r in ordered} == {r["slug"] for r in shuffled}


# --- cost -------------------------------------------------------------------


def test_a_board_that_yields_nothing_goes_quiet_after_the_grace_period(_isolated_db):
    """Paycom's shape: healthy HTTP, real postings, never a software one."""
    conn = _isolated_db
    _register(conn, {"live": ["dealership"]})

    for _ in range(db.ZERO_YIELD_GRACE - 1):
        db.mark_board_yield(conn, "fake", "dealership", 0)
    assert db.quiet_boards(conn, "fake") == set(), "backed off too early"

    db.mark_board_yield(conn, "fake", "dealership", 0)
    assert db.quiet_boards(conn, "fake") == {"dealership"}


def test_one_kept_posting_restores_full_frequency(_isolated_db):
    """Backoff, not exclusion — a dealership can hire a developer."""
    conn = _isolated_db
    _register(conn, {"live": ["dealership"]})
    for _ in range(db.ZERO_YIELD_GRACE + 2):
        db.mark_board_yield(conn, "fake", "dealership", 0)
    assert db.quiet_boards(conn, "fake") == {"dealership"}

    db.mark_board_yield(conn, "fake", "dealership", 1)

    assert db.quiet_boards(conn, "fake") == set()
    row = db.get_companies(conn, ats="fake")[0]
    assert row["zero_yield_streak"] == 0


def test_the_quiet_period_is_bounded(_isolated_db):
    """Two weeks, so a board that starts hiring engineers is seen that month."""
    conn = _isolated_db
    _register(conn, {"live": ["dealership"]})
    for _ in range(40):
        db.mark_board_yield(conn, "fake", "dealership", 0)

    row = db.get_companies(conn, ats="fake")[0]
    quiet_until = row["quiet_until"]
    if isinstance(quiet_until, str):
        quiet_until = datetime.fromisoformat(quiet_until)
    ceiling = datetime.now(timezone.utc) + timedelta(hours=db.ZERO_YIELD_MAX_HOURS)
    assert quiet_until <= ceiling + timedelta(minutes=1)


def test_a_quiet_board_is_skipped_and_all_boards_overrides(_isolated_db):
    conn = _isolated_db
    _register(conn, {"live": ["dealership", "software-co"]})
    for _ in range(db.ZERO_YIELD_GRACE):
        db.mark_board_yield(conn, "fake", "dealership", 0)
    source = FakeBoard({"software-co": [_job("software-co")]})

    totals = asyncio.run(_sweep(conn, source, _args(unchecked=0)))
    assert source.asked == ["software-co"]
    assert totals["quiet"] == 1

    source.asked.clear()
    asyncio.run(_sweep(conn, source, _args(unchecked=0, all_boards=True)))
    assert set(source.asked) == {"dealership", "software-co"}


def test_an_empty_answer_counts_as_a_zero_yield_sweep(_isolated_db):
    """The branch Paycom takes: `fetch_board` returns `[]` because the prefilter
    already threw the postings away, having paid for them."""
    conn = _isolated_db
    _register(conn, {"live": ["dealership"]})
    source = FakeBoard({"dealership": []})

    for _ in range(db.ZERO_YIELD_GRACE):
        asyncio.run(_sweep(conn, source, _args(unchecked=0, all_boards=True)))

    assert db.quiet_boards(conn, "fake") == {"dealership"}


def test_a_failing_board_is_not_treated_as_a_quiet_one(_isolated_db):
    """Two mechanisms, two meanings. A board answering `None` did not respond;
    that is board_health's cooldown, and reading it as "no software jobs here"
    would let an outage look like a dealership."""
    conn = _isolated_db
    _register(conn, {"live": ["broken"]})
    source = FakeBoard({"broken": None})

    for _ in range(db.ZERO_YIELD_GRACE + 1):
        asyncio.run(_sweep(conn, source, _args(unchecked=0, all_boards=True)))

    row = db.get_companies(conn, ats="fake")[0]
    assert row["zero_yield_streak"] == 0
    assert row["consecutive_failures"] >= db.ZERO_YIELD_GRACE


# --- blocked employers never cost a request ---------------------------------


def test_a_blocked_board_is_never_asked_for_anything(_isolated_db, monkeypatch):
    """The point of filtering at the board level rather than after the fetch.

    For an ATS board the company IS the slug, so it is knowable with no network
    at all — unlike the workplace filter, which reads a value derived from the
    description and so cannot save the request that fetched it. Measured on the
    live registry, this skips `greenhouse/stripe` (544 postings),
    `greenhouse/airbnb` (184) and `greenhouse/coinbase` (160) every sweep.
    """
    from jobscrape import filters

    conn = _isolated_db
    monkeypatch.setattr(filters, "BLOCKED_COMPANIES", frozenset({"Stripe", "Airbnb"}))
    _register(conn, {"live": ["stripe", "airbnb", "acme", "stripes"]})
    source = FakeBoard({s: [_job(s)] for s in ("stripe", "airbnb", "acme", "stripes")})

    asyncio.run(_sweep(conn, source, _args(unchecked=0)))

    # `stripes` is a different company and must survive — the registry really
    # does contain a board by that name.
    assert sorted(source.asked) == ["acme", "stripes"]


def test_a_blocked_board_is_not_probed_by_validate(_isolated_db, monkeypatch):
    """Probing a board we will never fetch wastes a request every pass, and
    thousands of unvalidated slugs are competing for that budget."""
    import asyncio as _asyncio

    from jobscrape import filters
    from jobscrape.discovery import validate as validate_mod

    conn = _isolated_db
    monkeypatch.setattr(filters, "BLOCKED_COMPANIES", frozenset({"Coinbase"}))
    _register(conn, {"unchecked": ["coinbase", "acme"]})
    probed: list[str] = []

    class Probing(FakeBoard):
        async def probe(self, fetcher, slug):
            probed.append(slug)
            return "live", 1

    source = Probing({})
    monkeypatch.setitem(validate_mod.BOARD_SOURCES, "fake", source)
    _asyncio.run(validate_mod.validate_ats(conn, None, "fake", concurrency=1))

    assert probed == ["acme"]
