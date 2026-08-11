"""Repost / cross-source duplicate collapse.

Three-tier policy, measured against the live archive rather than assumed —
see `fingerprint.py` and the plan doc for the numbers behind tiers 1/2:

* **Same source, strict `role_fingerprint`** (company + title + description):
  catches true reposts (one employer, one role, posted more than once).
  Zero over-merge risk, because the description has to match exactly.
* **Across sources, loose `role_key`** (company + title only, no description):
  needed because aggregators hard-truncate descriptions, so a strict match can
  never fire cross-source. This is looser and therefore only ever allowed to
  suppress an aggregator (`GLOBAL_SOURCES`) copy in favour of a direct ATS
  (`BOARD_SOURCES`) copy — it can never merge two ATS requisitions into each
  other, which measurement showed would wrongly collapse hundreds of distinct
  real postings.
* **Across sources, fuzzy title/description similarity** (tier 3, opt-in
  only — see `compute_clusters`'s `fuzzy` parameter and `cli.py`'s `--fuzzy`
  flag): catches the cases tier 2's exact `role_key` still misses — an
  aggregator's copy worded even slightly differently from the employer's own
  ("Software Engineer" vs "Software Engineer II"), or with a description too
  thin to compare. Same aggregator-to-ATS-only direction as tier 2, same
  reasoning: a wrong merge here silently collapses two distinct real
  postings, which is why it stays opt-in rather than running unconditionally
  like tiers 1/2.

Suppression is a `duplicate_of` pointer, never a delete: a mis-cluster is
reversible with `db.clear_duplicate`, and `job_status` — keyed independently of
`jobs` — survives regardless of which copy in a cluster ends up canonical.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any, Iterable

from . import db
from .fingerprint import normalize_text
from .sources import BOARD_SOURCES, restricted_board_sources


def _known_board_sources() -> frozenset[str]:
    """ATS names tier 2 must trust as "a direct board", not an aggregator.

    `BOARD_SOURCES` alone excludes `restricted_board_sources()` (Avature) --
    that registry is opt-in and imported lazily so a breakage there can never
    affect a normal run, but it is still a genuine per-company board, same
    trust level as Greenhouse or Lever once collected. Tier 2's entire safety
    guarantee is "never merge two ATS requisitions into each other" — missing
    Avature here read its rows as aggregator-provenance instead, so an
    Avature posting could be silently suppressed by another board's
    canonical, exactly the outcome that guarantee exists to prevent.
    """
    return frozenset(BOARD_SOURCES) | frozenset(restricted_board_sources())

log = logging.getLogger("jobscrape.dedupe")

#: What a freshly-resolved (no assignments considered) result counter looks
#: like, so callers never have to special-case an empty scope. tier3_* stays
#: at 0 whenever `fuzzy=False` (the default) rather than being absent, so a
#: caller never has to special-case "was tier 3 even attempted" either.
_EMPTY_COUNTS = {
    "candidates": 0, "collapsed": 0,
    "tier1_clusters": 0, "tier1_rows": 0,
    "tier2_clusters": 0, "tier2_rows": 0,
    "tier3_clusters": 0, "tier3_rows": 0,
}

_CANDIDATE_COLUMNS = (
    "id, source, company_slug, posted_at, first_seen_at, role_fingerprint, role_key"
)

#: Only pulled in when `fuzzy=True` — title/description text is otherwise
#: never needed by tiers 1/2, which key off precomputed hash columns alone.
#: Fetching these for the whole archive is real extra weight, which is the
#: other reason (besides false-positive risk) tier 3 stays opt-in.
_FUZZY_EXTRA_COLUMNS = "title, description_text, location_raw"

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: A tier-3 title near-match is allowed exactly one of these as its ENTIRE
#: difference from its counterpart — "Software Engineer" vs "Software
#: Engineer II", "Senior Data Engineer" vs "Data Engineer". Anything else
#: differing (a real word like "Manager") must clear the base Jaccard bar on
#: its own merits, not this allowance.
_SENIORITY_MODIFIERS = frozenset({
    "ii", "iii", "iv", "v", "2", "3", "senior", "sr", "junior", "jr",
})

#: Base title-token Jaccard similarity to count as the same role.
_TIER3_TITLE_THRESHOLD = 0.75
#: Higher bar required after the one-seniority-token allowance above is
#: applied — the allowance should rescue a near-miss, not lower the bar.
_TIER3_TITLE_RETRY_THRESHOLD = 0.85
#: Description-token Jaccard similarity, used only when neither row's own
#: `location_raw` gives a cheaper signal (the common case a thin aggregator
#: record forces this whole tier to exist for).
_TIER3_DESC_THRESHOLD = 0.82
#: Bound the per-comparison cost of the description fallback — Jaccard over
#: two multi-thousand-character bodies is wasted precision this tier doesn't
#: need, since the threshold is decisive well within the opening paragraphs.
_TIER3_DESC_COMPARE_CHARS = 1000

#: A company-slug group above this size is skipped for tier 3 entirely
#: (logged, not silently dropped) rather than paying for it — a handful of
#: very large employers would otherwise dominate a `--fuzzy` backfill's
#: runtime for a heuristic that is opt-in in the first place. Tiers 1/2
#: still run on these rows regardless; only the fuzzy comparison is capped.
_TIER3_MAX_GROUP_SIZE = 250


def _tokens(text: str) -> frozenset[str]:
    """Word tokens from already-normalized (lowercased, whitespace-
    collapsed) text — the raw material every tier-3 similarity check
    compares. Punctuation is deliberately not preserved: tier 3 wants
    "C++" and "C++," to tokenize identically, unlike the exact-hash tiers
    above, which normalize shallowly on purpose (see `fingerprint.py`)."""
    return frozenset(_TOKEN_RE.findall(text))


def _title_tokens(title: str | None) -> frozenset[str]:
    return _tokens(normalize_text(title))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _titles_match(a_tokens: frozenset[str], b_tokens: frozenset[str]) -> bool:
    """Same role, allowing for one seniority/numeral qualifier's difference.

    "Software Engineer" vs "Software Engineer II" passes on retry.
    "Backend Engineer" vs "Backend Engineering Manager" fails both passes —
    "engineering" and "manager" are two real, non-modifier differences, not
    one allowed qualifier.
    """
    if not a_tokens or not b_tokens:
        return False
    if _jaccard(a_tokens, b_tokens) >= _TIER3_TITLE_THRESHOLD:
        return True
    larger, smaller = (
        (a_tokens, b_tokens) if len(a_tokens) >= len(b_tokens) else (b_tokens, a_tokens)
    )
    extra = larger - smaller
    if len(extra) == 1 and extra <= _SENIORITY_MODIFIERS:
        return _jaccard(larger - extra, smaller) >= _TIER3_TITLE_RETRY_THRESHOLD
    return False


def _locations_overlap(a: str | None, b: str | None) -> bool:
    a_tokens = _tokens(normalize_text(a))
    b_tokens = _tokens(normalize_text(b))
    return bool(a_tokens) and bool(b_tokens) and bool(a_tokens & b_tokens)


def _descriptions_match(a: str | None, b: str | None) -> bool:
    a_tokens = _tokens(normalize_text(a)[:_TIER3_DESC_COMPARE_CHARS])
    b_tokens = _tokens(normalize_text(b)[:_TIER3_DESC_COMPARE_CHARS])
    if not a_tokens or not b_tokens:
        return False
    return _jaccard(a_tokens, b_tokens) >= _TIER3_DESC_THRESHOLD


def _sort_key(row: dict[str, Any]) -> tuple:
    """Canonical-selection order: most recent posted_at, then most recent
    first_seen_at, then lowest id — purely for determinism. A row with no
    posted_at is treated as older than any row that has one, since "most
    recent" should mean an actual, known date whenever one is available.

    Tuple comparison never needs to order two ``None`` values against each
    other: the boolean guard in front of each field means the raw values are
    only ever compared once both sides are known to be present (or both
    absent, in which case they're equal and comparison moves on).
    """
    posted = row.get("posted_at")
    first_seen = row.get("first_seen_at")
    return (posted is not None, posted, first_seen is not None, first_seen, -row["id"])


def _canonical(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(rows, key=_sort_key)


def _slug_key(slug: str | None) -> str:
    """A company slug reduced to what two sources can be expected to agree on.

    See the comment at the tier-2 grouping for why. Empty string rather than
    ``None`` so it is a usable dict key; rows with no slug group together and
    are then separated by `role_key`, which is the behaviour this replaced.
    """
    from .sources.apify.ats_link import normalise_slug

    return normalise_slug(slug) or ""


def _fuzzy_row_match(
    agg_row: dict[str, Any], ats_row: dict[str, Any], agg_title_tokens: frozenset[str],
) -> bool:
    """One agg-vs-ats comparison: title similarity, then a location-or-
    description fallback — see the module docstring's tier-3 paragraph."""
    if not _titles_match(agg_title_tokens, _title_tokens(ats_row.get("title"))):
        return False
    agg_loc = agg_row.get("location_raw")
    ats_loc = ats_row.get("location_raw")
    if agg_loc and ats_loc:
        return _locations_overlap(agg_loc, ats_loc)
    # Common case this whole tier exists for: an aggregator record too thin
    # to carry a location at all. Fall back to description similarity rather
    # than treating "no location on one side" as a free pass.
    return _descriptions_match(
        agg_row.get("description_text"), ats_row.get("description_text"),
    )


def _best_fuzzy_match(
    agg_row: dict[str, Any], ats_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The single ATS row `agg_row` should defer to, or ``None``.

    More than one ATS row can plausibly match (a company with several open
    reqs sharing a near-identical title) — `_canonical` breaks the tie the
    same way tier 2 already does, rather than picking whichever happened to
    be compared first.
    """
    agg_title_tokens = _title_tokens(agg_row.get("title"))
    if not agg_title_tokens:
        return None
    candidates = [
        ats for ats in ats_rows if _fuzzy_row_match(agg_row, ats, agg_title_tokens)
    ]
    return _canonical(candidates) if candidates else None


def _tier3_fuzzy(
    survivors: list[dict[str, Any]],
    assignments: dict[int, int],
    known_boards: frozenset[str],
    counts: dict[str, int],
) -> None:
    """Tier 3: bounded fuzzy title/description similarity, aggregator → ATS
    only — same directional safety tier 2 has, same reason (see module
    docstring). Mutates `assignments`/`counts` in place.

    Grouped by company slug ALONE (looser than tier 2's exact `role_key` —
    rows already sharing that key are already exact matches, so fuzzy
    comparison there would be a no-op) and restricted to rows tiers 1/2 left
    untouched. Only ever compares aggregator rows against ATS rows within
    the SAME company, never agg-vs-agg or ats-vs-ats — this bounds the cost
    to `sum over companies of (ats_count × agg_count)`, not a global n²
    scan, on top of the explicit `_TIER3_MAX_GROUP_SIZE` cap below.
    """
    by_slug: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in survivors:
        if r["id"] in assignments:
            continue
        slug = _slug_key(r.get("company_slug"))
        if slug:
            by_slug[slug].append(r)

    for group in by_slug.values():
        if len(group) > _TIER3_MAX_GROUP_SIZE:
            log.info(
                "dedupe tier3: skipping a %d-row company group (over the "
                "%d-row cap) — tiers 1/2 already ran on it",
                len(group), _TIER3_MAX_GROUP_SIZE,
            )
            continue
        ats_rows = [r for r in group if r["source"] in known_boards]
        agg_rows = [r for r in group if r["source"] not in known_boards]
        if not ats_rows or not agg_rows:
            continue
        matched_any = False
        for agg in agg_rows:
            match = _best_fuzzy_match(agg, ats_rows)
            if match is not None:
                assignments[agg["id"]] = match["id"]
                counts["tier3_rows"] += 1
                matched_any = True
        if matched_any:
            counts["tier3_clusters"] += 1


def compute_clusters(
    rows: list[dict[str, Any]], *, fuzzy: bool = False,
) -> tuple[dict[int, int], dict[str, int]]:
    """Pure function: candidate rows in, ``{duplicate_id: canonical_id}`` out.

    Deliberately takes no database connection — the part of this feature with
    real behavioral risk (which rows collapse into which) is fully testable
    without one. Each row needs ``id``, ``source``, ``company_slug``,
    ``posted_at``, ``first_seen_at``, ``role_fingerprint``, ``role_key`` —
    and, only when ``fuzzy=True``, ``title``, ``description_text``,
    ``location_raw`` too (tier 3's own raw material, see
    ``_FUZZY_EXTRA_COLUMNS``).
    """
    assignments: dict[int, int] = {}
    counts = dict(_EMPTY_COUNTS)
    counts.pop("candidates")
    counts.pop("collapsed")

    # Tier 1: same source, strict fingerprint.
    by_source_fp: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    no_fingerprint: list[dict[str, Any]] = []
    for r in rows:
        fp = r.get("role_fingerprint")
        (by_source_fp[(r["source"], fp)] if fp else no_fingerprint).append(r)

    survivors: list[dict[str, Any]] = list(no_fingerprint)
    for group in by_source_fp.values():
        if len(group) < 2:
            survivors.extend(group)
            continue
        canon = _canonical(group)
        counts["tier1_clusters"] += 1
        for r in group:
            if r["id"] == canon["id"]:
                survivors.append(r)
            else:
                assignments[r["id"]] = canon["id"]
                counts["tier1_rows"] += 1

    # Tier 2: cross-source, loose key, aggregator -> ATS only. Operates on
    # tier-1 survivors only (rows tier 1 already suppressed stay suppressed
    # by their tier-1 canonical, not re-pointed at a tier-2 one).
    # The company slug is normalised for the KEY only, never rewritten on the
    # row. An ATS row's slug is the board slug (`10alabs`); an aggregator
    # derives it from the company name (`10a-labs`). Those describe the same
    # employer and never matched, so the clusters that most needed collapsing
    # were the ones that silently did not form — measured before this change,
    # Adzuna left 57% of its rows unsuppressed and Jooble 85%.
    #
    # Stripping to alphanumerics is deliberately blunt. It cannot merge two
    # genuinely different employers unless their names differ only by
    # punctuation, and it is applied to both sides of a comparison rather than
    # stored, so the slug a board URL is built from is untouched.
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in survivors:
        key = r.get("role_key")
        if key:
            by_key[(_slug_key(r.get("company_slug")), key)].append(r)

    known_boards = _known_board_sources()
    for group in by_key.values():
        ats_rows = [r for r in group if r["source"] in known_boards]
        agg_rows = [r for r in group if r["source"] not in known_boards]
        if not ats_rows or not agg_rows:
            continue
        canon = _canonical(ats_rows)
        counts["tier2_clusters"] += 1
        for r in agg_rows:
            assignments[r["id"]] = canon["id"]
            counts["tier2_rows"] += 1

    # Tier 3: opt-in only — see the module docstring and `_tier3_fuzzy`.
    if fuzzy:
        _tier3_fuzzy(survivors, assignments, known_boards, counts)

    return _compress_chains(assignments), counts


def _compress_chains(assignments: dict[int, int]) -> dict[int, int]:
    """Rewrite every assignment to point at its ultimate root.

    A tier-1 canonical can itself get reassigned by tier 2 (its own cluster
    turns out to have an ATS row it should defer to) — without this, that
    row's tier-1 dependents would end up pointing at a row that is *itself* a
    duplicate, a two-hop chain that breaks the invariant every read path
    relies on: "canonical" means ``duplicate_of IS NULL``, full stop, not
    "follow the chain to find out."
    """
    resolved: dict[int, int] = {}

    def root(node_id: int) -> int:
        if node_id not in assignments:
            return node_id
        if node_id in resolved:
            return resolved[node_id]
        # `assignments` only ever grows by one hop per tier (tier1 then
        # tier2), so this recursion is bounded by the number of tiers, not by
        # cluster size — safe regardless of how large a cluster is.
        result = root(assignments[node_id])
        resolved[node_id] = result
        return result

    return {node_id: root(node_id) for node_id in assignments}


def _apply(
    conn: db.Connection, rows: list[dict[str, Any]], *, dry_run: bool, fuzzy: bool = False,
) -> dict[str, Any]:
    assignments, counts = compute_clusters(rows, fuzzy=fuzzy)
    if not dry_run and assignments:
        # `duplicate_marked_at` records the transition, not just the current
        # value -- it's what lets `render_sync` notice a row just became
        # suppressed. `last_seen_at`/`closed_at` don't change here, so
        # without this a row already synced to Render before this clustering
        # ran would stay there forever: `duplicate_of IS NOT NULL` excludes
        # it from every later render-sync pass's "what changed" query.
        now = db.now_iso()
        conn.executemany(
            "UPDATE jobs SET duplicate_of = ?, duplicate_marked_at = ? WHERE id = ?",
            [(canon_id, now, dup_id) for dup_id, canon_id in assignments.items()],
        )
        conn.commit()
    return {"candidates": len(rows), "collapsed": len(assignments), **counts}


def resolve_scoped(conn: db.Connection, company_slug: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Recompute clusters for one company only — an index range scan.

    This is the hot path: called synchronously after `upsert_jobs` for the
    slug(s) just touched, so a board fetch never pays for a full-table scan.
    """
    if not company_slug:
        return dict(_EMPTY_COUNTS)
    rows = conn.execute(
        f"SELECT {_CANDIDATE_COLUMNS} FROM jobs "
        "WHERE closed_at IS NULL AND duplicate_of IS NULL AND company_slug = ?",
        (company_slug,),
    ).fetchall()
    return _apply(conn, rows, dry_run=dry_run)


def resolve_many(conn: db.Connection, company_slugs: Iterable[str]) -> dict[str, Any]:
    """`resolve_scoped` for each of several slugs, with combined counters."""
    total = dict(_EMPTY_COUNTS)
    for slug in {s for s in company_slugs if s}:
        result = resolve_scoped(conn, slug)
        for k in total:
            total[k] += result[k]
    return total


def resolve_all(conn: db.Connection, *, dry_run: bool = False, fuzzy: bool = False) -> dict[str, Any]:
    """Full-archive recompute — the backfill path (`jobscrape dedupe --backfill`).

    Safe to call repeatedly: previously-suppressed rows are excluded from
    consideration, so a fresh recompute only ever adds new suppressions, never
    removes one — clearing a duplicate is `db.clear_duplicate`, a deliberate,
    separate action.

    ``fuzzy=True`` also runs tier 3 (see the module docstring) — opt-in only,
    off by default, and only ever reachable through this full-archive path:
    `resolve_scoped`/`resolve_many` (the hot, per-company path called after
    every `upsert_jobs`) take no `fuzzy` parameter at all, so tier 3 can
    never run there even by mistake.
    """
    columns = _CANDIDATE_COLUMNS + (f", {_FUZZY_EXTRA_COLUMNS}" if fuzzy else "")
    rows = conn.execute(
        f"SELECT {columns} FROM jobs WHERE closed_at IS NULL AND duplicate_of IS NULL"
    ).fetchall()
    return _apply(conn, rows, dry_run=dry_run, fuzzy=fuzzy)


def sample_clusters(conn: db.Connection, limit: int = 10) -> list[dict[str, Any]]:
    """A handful of resolved clusters, for eyeballing before/after a backfill."""
    canon_ids = [
        r["duplicate_of"]
        for r in conn.execute(
            "SELECT DISTINCT duplicate_of FROM jobs WHERE duplicate_of IS NOT NULL "
            "ORDER BY duplicate_of LIMIT ?",
            (limit,),
        )
    ]
    clusters = []
    for cid in canon_ids:
        members = conn.execute(
            "SELECT id, source, company, title, posted_at, duplicate_of "
            "FROM jobs WHERE id = ? OR duplicate_of = ? ORDER BY (duplicate_of IS NULL) DESC, id",
            (cid, cid),
        ).fetchall()
        clusters.append({"canonical_id": cid, "members": members})
    return clusters
