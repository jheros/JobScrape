"""Tests for slug harvesting and cleanup."""

from __future__ import annotations

import json
from typing import Any

import pytest

from jobscrape.discovery import commoncrawl
from jobscrape.discovery.commoncrawl import (
    TARGETS,
    WORKDAY_HOST_RE,
    _clean,
    _dedupe_workday_case_variants,
    _load_progress,
    _save_progress,
    harvest_ats,
)
from jobscrape.discovery.seeds import SEED_BOARDS


def test_junk_slugs_rejected() -> None:
    for junk in [
        "12345", "robots.txt", "sitemap.xml", "embed", "api", "v1",
        "favicon.ico", "style.css", "app.js", "", "x",
        "5364856uhdfnvbkldfnbhrpkdfgbdvtyhro",  # no vowels: a hash, not a company
    ]:
        assert _clean(junk) is None, f"{junk!r} should be rejected"


def test_real_slugs_kept() -> None:
    for good in ["stripe", "airbnb", "1kosmos", "2020companies", "ace-it-careers-1",
                 "modern-treasury", "10xgenomics"]:
        assert _clean(good) == good.lower(), f"{good!r} should be kept"


def test_workday_regex_skips_locale_segment() -> None:
    """The locale prefix must not be mistaken for the career-site name."""
    url = ("https://2020companies.wd1.myworkdayjobs.com/en-US/External_Careers"
           "/job/Bilingual-BOH-Specialist_REQ_097039")
    match = WORKDAY_HOST_RE.search(url)
    assert match is not None
    assert match.groups() == ("2020companies", "wd1", "External_Careers")


def test_workday_regex_without_locale() -> None:
    url = "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/x_JR1"
    match = WORKDAY_HOST_RE.search(url)
    assert match is not None
    assert match.groups() == ("nvidia", "wd5", "NVIDIAExternalCareerSite")


def test_workday_regex_ignores_impl_tenants() -> None:
    """Sandbox hosts like impl-wd502 are not real career sites."""
    assert WORKDAY_HOST_RE.search("https://sotel.impl-wd502.myworkdayjobs.com/robots.txt") is None


def test_case_variant_sites_collapse() -> None:
    """Probing both `Search` and `search` doubles the work for one board."""
    result = _dedupe_workday_case_variants({"3m|wd1|Search", "3m|wd1|search"})
    assert result == {"3m|wd1|Search"}, "the mixed-case spelling is the real one"


def test_genuinely_distinct_sites_are_kept() -> None:
    """Different career sites under one tenant hold different jobs."""
    slugs = {"aes|wd1|AES_US", "aes|wd1|AES_ANDES", "aes|wd1|AES_CHILE"}
    assert _dedupe_workday_case_variants(slugs) == slugs


def test_case_dedupe_is_order_independent() -> None:
    a = _dedupe_workday_case_variants({"x|wd1|Careers", "x|wd1|careers"})
    b = _dedupe_workday_case_variants({"x|wd1|careers", "x|wd1|Careers"})
    assert a == b == {"x|wd1|Careers"}


def test_targets_are_index_patterns() -> None:
    """Each target must be a Common Crawl URL pattern, not a bare host."""
    for pattern, ats, regex in TARGETS:
        assert pattern.endswith("/*"), f"{pattern} is not an index query pattern"
        assert ats and regex


def test_seeds_are_sane() -> None:
    assert SEED_BOARDS
    for ats, slugs in SEED_BOARDS.items():
        assert slugs, f"{ats} has no seeds"
        assert len(slugs) == len(set(slugs)), f"{ats} has duplicate seeds"
        for slug in slugs:
            assert slug == slug.strip().lower(), f"seed {slug!r} is not normalized"
            assert "/" not in slug and " " not in slug, f"seed {slug!r} is not a slug"


def test_seeds_bypass_the_junk_heuristics() -> None:
    """Seeds are pre-verified, so the discovery heuristics must not apply.

    `_clean` rejects all-numeric strings because the Common Crawl index is full
    of numeric ids — but "1001" is a real Breezy company. Since seeds are merged
    in directly rather than passing through `_clean`, such a company survives;
    routing seeds through the filter would silently drop it.
    """
    assert _clean("1001") is None, "the discovery heuristic still rejects numerics"
    numeric_seeds = [s for slugs in SEED_BOARDS.values() for s in slugs if s.isdigit()]
    assert numeric_seeds, "expected at least one all-numeric seed to guard this path"


# --- checkpoint: a page's slugs must survive a crash right after it's saved -

class _FakeCCFetcher:
    """Serves a fixed page count, then one JSON-lines page of URLs per call —
    tracks which pages were actually requested, so a test can prove a
    checkpointed page is genuinely never re-fetched."""

    def __init__(self, pages: int, page_bodies: dict[int, str]) -> None:
        self._pages = pages
        self._bodies = page_bodies
        self.requested_pages: list[int] = []

    async def get_json(self, url: str, params: dict[str, Any], **kw: Any) -> Any:
        return {"pages": self._pages}

    async def get_text(self, url: str, params: dict[str, Any], **kw: Any) -> Any:
        page = int(params["page"])
        self.requested_pages.append(page)
        return self._bodies.get(page)


@pytest.fixture()
def _isolated_progress_path(tmp_path, monkeypatch):
    path = tmp_path / "cc_progress.json"
    monkeypatch.setattr(commoncrawl, "PROGRESS_PATH", path)
    return path


def test_save_and_load_progress_round_trips_done_and_results(
    _isolated_progress_path,
) -> None:
    done = {"2026-30|boards.greenhouse.io/*|0"}
    results = {"greenhouse": {"acme", "globex"}}
    _save_progress(done, results)
    loaded_done, loaded_results = _load_progress()
    assert loaded_done == done
    assert loaded_results == results


def test_load_progress_tolerates_a_missing_or_corrupt_file(
    _isolated_progress_path,
) -> None:
    assert _load_progress() == (set(), {})
    _isolated_progress_path.write_text("not json", encoding="utf-8")
    assert _load_progress() == (set(), {})


async def test_harvest_ats_recovers_a_checkpointed_pages_slugs(
    _isolated_progress_path,
) -> None:
    """A page's slugs used to live only in the local `set` `harvest_ats`
    returned at the very end of a multi-hour sweep -- the checkpoint marked
    the page "done" as soon as it was extracted, but nothing persisted the
    slugs themselves until the WHOLE sweep finished. A crash between those
    two points left the checkpoint claiming a page was done while its
    slugs existed only in the crashed process's memory: a resumed run
    skipped the page (correctly, per the checkpoint) but never re-extracted
    it either, silently losing every company on it.

    This simulates exactly that: page 0 is already marked done, its
    previously-found slug is seeded into `results` (as `discover_all` now
    does from the checkpoint), and only page 1 is actually fetchable.
    """
    url_pattern, ats, pattern = TARGETS[1]  # boards.greenhouse.io
    assert ats == "greenhouse"

    done = {f"CC-MAIN-2026|{url_pattern}|0"}
    results = {"greenhouse": {"already-found-co"}}
    fetcher = _FakeCCFetcher(
        pages=2,
        page_bodies={
            1: json.dumps({"url": "https://boards.greenhouse.io/newco/jobs/1"}),
        },
    )

    slugs = await harvest_ats(
        fetcher, "CC-MAIN-2026", url_pattern, ats, pattern,
        max_pages=None, done=done, results=results,
    )

    assert 0 not in fetcher.requested_pages, "a checkpointed page must never be re-fetched"
    assert 1 in fetcher.requested_pages
    # Both the recovered (page 0, from the checkpoint) and freshly harvested
    # (page 1) slugs must be present -- neither lost, neither duplicated away.
    assert slugs == {"already-found-co", "newco"}
    assert results["greenhouse"] == {"already-found-co", "newco"}
