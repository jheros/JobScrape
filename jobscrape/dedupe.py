"""Repost / cross-source duplicate collapse.

Two-tier policy, measured against the live archive rather than assumed — see
`fingerprint.py` and the plan doc for the numbers behind this design:

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

Suppression is a `duplicate_of` pointer, never a delete: a mis-cluster is
reversible with `db.clear_duplicate`, and `job_status` — keyed independently of
`jobs` — survives regardless of which copy in a cluster ends up canonical.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Iterable

from . import db
from .sources import BOARD_SOURCES

log = logging.getLogger("jobscrape.dedupe")

#: What a freshly-resolved (no assignments considered) result counter looks
#: like, so callers never have to special-case an empty scope.
_EMPTY_COUNTS = {
    "candidates": 0, "collapsed": 0,
    "tier1_clusters": 0, "tier1_rows": 0,
    "tier2_clusters": 0, "tier2_rows": 0,
}

_CANDIDATE_COLUMNS = (
    "id, source, company_slug, posted_at, first_seen_at, role_fingerprint, role_key"
)


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


def compute_clusters(rows: list[dict[str, Any]]) -> tuple[dict[int, int], dict[str, int]]:
    """Pure function: candidate rows in, ``{duplicate_id: canonical_id}`` out.

    Deliberately takes no database connection — the part of this feature with
    real behavioral risk (which rows collapse into which) is fully testable
    without one. Each row needs ``id``, ``source``, ``company_slug``,
    ``posted_at``, ``first_seen_at``, ``role_fingerprint``, ``role_key``.
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
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in survivors:
        key = r.get("role_key")
        if key:
            by_key[(r.get("company_slug") or "", key)].append(r)

    for group in by_key.values():
        ats_rows = [r for r in group if r["source"] in BOARD_SOURCES]
        agg_rows = [r for r in group if r["source"] not in BOARD_SOURCES]
        if not ats_rows or not agg_rows:
            continue
        canon = _canonical(ats_rows)
        counts["tier2_clusters"] += 1
        for r in agg_rows:
            assignments[r["id"]] = canon["id"]
            counts["tier2_rows"] += 1

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


def _apply(conn: db.Connection, rows: list[dict[str, Any]], *, dry_run: bool) -> dict[str, Any]:
    assignments, counts = compute_clusters(rows)
    if not dry_run and assignments:
        conn.executemany(
            "UPDATE jobs SET duplicate_of = ? WHERE id = ?",
            [(canon_id, dup_id) for dup_id, canon_id in assignments.items()],
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


def resolve_all(conn: db.Connection, *, dry_run: bool = False) -> dict[str, Any]:
    """Full-archive recompute — the backfill path (`jobscrape dedupe --backfill`).

    Safe to call repeatedly: previously-suppressed rows are excluded from
    consideration, so a fresh recompute only ever adds new suppressions, never
    removes one — clearing a duplicate is `db.clear_duplicate`, a deliberate,
    separate action.
    """
    rows = conn.execute(
        f"SELECT {_CANDIDATE_COLUMNS} FROM jobs WHERE closed_at IS NULL AND duplicate_of IS NULL"
    ).fetchall()
    return _apply(conn, rows, dry_run=dry_run)


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
