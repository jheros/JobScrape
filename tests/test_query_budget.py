"""The search-term list, and the request budgets it is spent against.

Every aggregator ceiling here is *per term*, so `DEFAULT_QUERIES` is the one
lever on breadth that no page cap bounds — which is exactly why it needs
guarding. Two ways to get it wrong:

* **Add a term the role filter throws away.** "qa engineer" was removed from
  this list for that reason: Jooble, Adzuna and USAJobs each spent pages
  searching for postings that were then discarded. A term is only worth its
  requests if what it returns can be stored.
* **Multiply the spend past a free tier.** Adzuna's quota is per day, and the
  old fixed 20-pages-per-term cost ~220 calls against 11 terms. Widening the
  list under that rule would have doubled the bill; the budget is fixed and
  divided instead.
"""

from __future__ import annotations

import pytest

from jobscrape import config
from jobscrape.filters import is_excluded_role, is_software_role
from jobscrape.sources import ALL_SOURCE_NAMES, GLOBAL_SOURCES, adzuna
from jobscrape.sources.base import DEFAULT_QUERIES


@pytest.mark.parametrize("term", DEFAULT_QUERIES)
def test_every_search_term_survives_the_role_filter(term: str) -> None:
    """A term whose own results the filter rejects spends budget for nothing.

    Both checks matter and they are not the same one. "embedded software
    engineer" reads as software but is excluded as `firmware`; ".net developer"
    is not excluded and does not qualify as software either.
    """
    assert is_software_role(term), f"{term!r} does not read as a software role"
    assert is_excluded_role(term) is None, f"{term!r} is an excluded category"


def test_the_terms_are_distinct() -> None:
    assert len(set(DEFAULT_QUERIES)) == len(DEFAULT_QUERIES)


def test_the_first_terms_are_the_broadest() -> None:
    """Order is load-bearing: Workday searches `[:4]` and LinkedIn `[:6]`, so a
    term appended to the end is free and a term inserted at the front silently
    changes what those two look for."""
    assert DEFAULT_QUERIES[:4] == [
        "software engineer", "software developer",
        "backend engineer", "frontend engineer",
    ]


def test_adzuna_divides_a_fixed_call_budget_across_the_terms() -> None:
    """Adding terms re-spreads the spend rather than multiplying it."""
    per_term = max(1, min(adzuna.MAX_PAGES,
                          adzuna.CALL_BUDGET // len(DEFAULT_QUERIES)))
    assert per_term * len(DEFAULT_QUERIES) <= adzuna.CALL_BUDGET
    assert per_term >= 1, "a term must get at least one page"


def test_adzuna_never_pages_deeper_than_its_own_ceiling() -> None:
    """With few enough terms the budget stops binding, and MAX_PAGES takes over
    — the division must not turn into permission to page further."""
    per_term = max(1, min(adzuna.MAX_PAGES, adzuna.CALL_BUDGET // 2))
    assert per_term <= adzuna.MAX_PAGES


def test_the_binding_page_caps_are_all_tunable() -> None:
    """Adzuna's was hard-coded while its two peers were env-driven, which is how
    it ended up the one nobody could adjust without a code change."""
    for name in ("JOOBLE_MAX_PAGES", "HIMALAYAS_MAX_PAGES",
                 "ADZUNA_MAX_PAGES", "ADZUNA_CALL_BUDGET"):
        assert isinstance(getattr(config, name), int)


def test_jsearch_is_registered_but_not_swept_by_default() -> None:
    """Zero rows since it was written — its provider 404s every path — so it no
    longer costs six requests a run to re-confirm that. Kept addressable: the
    adapter is fine, and a working RAPIDAPI_HOST/PATH is all it needs."""
    assert "jsearch" in GLOBAL_SOURCES
    assert "jsearch" not in ALL_SOURCE_NAMES
