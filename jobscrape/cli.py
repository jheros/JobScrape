"""Command-line interface: discover -> validate -> fetch -> export."""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import os
import sys
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any

from . import config, db, dedupe, export as export_mod
from .observability import setup_logging
from .discovery import (
    certtransparency,
    commoncrawl,
    github_lists,
    validate as validate_mod,
)
from . import filters as apply_filters_mod
from .filters import (
    EXCLUDED_ROLE_NAMES,
    FilterFunnel,
    apply_filters_counted,
    cheap_prefilter,
    parse_date,
)
from .http import Fetcher
from .sources import (
    ALL_SOURCE_NAMES,
    BOARD_SOURCES,
    GLOBAL_SOURCES,
    restricted_board_sources,
    restricted_sources,
)
from .sources.base import is_complete

log = logging.getLogger("jobscrape")


def _allowed_roles(args: argparse.Namespace) -> set[str]:
    """Excluded categories the caller asked to re-admit.

    `--include-roles` is the general form. `--include-qa` and
    `--include-management` predate it, are documented in the README, and are
    kept as aliases rather than broken.
    """
    allowed = set(getattr(args, "include_roles", None) or ())
    if getattr(args, "include_qa", False):
        allowed.add("qa")
    if getattr(args, "include_management", False):
        allowed.add("management")
    return allowed


def _command_line() -> str:
    """The invocation, recorded with each run so history is reproducible."""
    return " ".join(sys.argv[1:]) or "(no args)"

#: Board ATSes selectable on the command line. Restricted boards are
#: included so their discovered slugs can be validated on request, even
#: though a normal fetch leaves them out.
_ATS_CHOICES = sorted(set(BOARD_SOURCES) | {"avature"})

#: How many cooled boards to probe before concluding a platform is still down.
#: Bounded because the probe runs every sweep; three is enough to distinguish
#: "the platform is back" from "this one board is genuinely broken".
MAX_RECOVERY_PROBES = 3

#: How many not-yet-validated boards each sweep pulls in per ATS, on top of
#: every live one.
#:
#: Not unbounded, and not zero. Zero is what the old `live`-only branch did to
#: any partially validated platform — 16,866 discovered boards across six
#: platforms were unreachable, iCIMS sweeping 6 of 2,499. Unbounded is what the
#: old zero-live branch did — every sweep spent ~10,000 requests on Paycom's
#: 5,135 portals, which return real postings that are never software.
#:
#: 500 per ATS per sweep works through the largest backlog here (BambooHR's
#: 5,629) in eleven runs, and boards that answer are promoted to `live`
#: immediately, so the slice keeps moving rather than re-drawing the same slugs.
UNCHECKED_PER_ATS = 500


# --------------------------------------------------------------------------
# discover
# --------------------------------------------------------------------------

async def cmd_discover(args: argparse.Namespace) -> int:
    vectors = set(args.vector or ["cc", "ct", "github"])
    results: dict[str, set[str]] = {}

    def merge(found: dict[str, set[str]]) -> None:
        for ats, slugs in found.items():
            results.setdefault(ats, set()).update(slugs)

    async with Fetcher(use_cache=not args.no_cache) as fetcher:
        if "cc" in vectors:
            log.info("Vector: Common Crawl index")
            merge(await commoncrawl.discover_all(
                fetcher,
                collections=args.collections,
                max_pages=args.max_pages,
                include_workday=not args.no_workday,
            ))
        if "ct" in vectors:
            # Independent of Common Crawl, so it still works when the index is
            # rate-limiting us — which it periodically does.
            log.info("Vector: certificate transparency")
            merge(await certtransparency.discover_all(fetcher))
        if "github" in vectors:
            log.info("Vector: curated GitHub job lists")
            merge(await github_lists.discover_all(fetcher))

    if not results:
        log.error("Discovery returned nothing from any vector.")
        return 1

    with db.session(args.db) as conn:
        total_new = 0
        for ats, slugs in sorted(results.items()):
            added = db.add_companies(conn, ats, sorted(slugs))
            total_new += added
            log.info("%-16s %6d found, %6d new", ats, len(slugs), added)
        log.info("Discovery complete: %d new companies added", total_new)
    return 0


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------

async def cmd_validate(args: argparse.Namespace) -> int:
    # Restricted boards are validated only when named explicitly, never as part
    # of a default sweep.
    known = set(BOARD_SOURCES) | set(restricted_board_sources())
    targets = args.ats or list(BOARD_SOURCES)
    with db.session(args.db) as conn:
        async with Fetcher(use_cache=not args.no_cache) as fetcher:
            for ats in targets:
                if ats not in known:
                    log.warning("Unknown ATS %r, skipping", ats)
                    continue
                log.info("Validating %s boards...", ats)
                await validate_mod.validate_ats(
                    conn, fetcher, ats,
                    limit=args.limit,
                    concurrency=args.concurrency,
                    recheck=args.recheck,
                    sample=getattr(args, "sample", False),
                )
    return 0


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------

async def _fetch_boards(
    conn: db.Connection,
    fetcher: Fetcher,
    ats: str,
    since: date | None,
    until: date | None,
    args: argparse.Namespace,
    boards: dict[str, Any] | None = None,
) -> tuple[dict[str, int], FilterFunnel]:
    source = (boards or BOARD_SOURCES)[ats]
    # Every live board, plus a bounded slice of the ones nobody has probed yet.
    #
    # This used to be `live`, falling back to `unchecked` ONLY when there were
    # zero live boards — all or nothing. A *partially* validated platform was
    # therefore pinned to its handful of known-live boards forever: iCIMS swept
    # 6 boards while 2,493 unchecked slugs sat beside them, Workday swept 71 of
    # 3,561, BambooHR 1 of 5,648. 16,866 discovered boards were unreachable
    # across six platforms, and no amount of re-running fetch would find them.
    #
    # The slice is capped and randomly ordered rather than unbounded, for two
    # reasons. Unbounded is what the old zero-live branch did, and it is why
    # every sweep spent ~10,000 requests on Paycom's 5,135 portals. And an
    # unchecked row has `job_count = 0`, so the default ordering is alphabetical
    # — an uncapped-but-limited slice would re-sweep the same slugs forever
    # instead of working through the backlog.
    rows = db.get_companies(conn, ats=ats, status="live", limit=args.companies)
    unchecked = getattr(args, "unchecked", UNCHECKED_PER_ATS)
    if unchecked:
        rows += db.get_companies(
            conn, ats=ats, status="unchecked", limit=unchecked, order="random",
        )
    empty = {"raw": 0, "kept": 0, "new": 0, "refreshed": 0, "boards": 0, "closed": 0,
             "cooled_down": 0, "quiet": 0}
    if not rows:
        log.info("%-16s no companies discovered yet", ats)
        return empty, FilterFunnel()

    cooldown = db.boards_in_cooldown(conn, ats)
    if cooldown and len(rows) > 1:
        # Half-open probe. A platform-wide outage backs off every board of that
        # platform independently, and each would then serve out up to 24h on
        # its own clock long after the outage ended. Probing a few cooled
        # boards costs at most MAX_RECOVERY_PROBES requests, and one success is
        # enough evidence that the platform is answering again — so it releases
        # all of them at once rather than waiting out the longest timer.
        #
        # Skipped for a single-board platform: a lone board has nothing to
        # release, so the probe would just be its retry with extra steps.
        probes = [r["slug"] for r in rows if r["slug"] in cooldown][:MAX_RECOVERY_PROBES]
        for slug in probes:
            try:
                probed = await source.fetch_board(fetcher, slug, with_details=False)
            except Exception:
                probed = None
            if probed is not None:
                cleared = db.clear_ats_cooldowns(conn, ats)
                conn.commit()
                log.info(
                    "%-16s recovered: %s answered, cleared %d cooldown(s)",
                    ats, slug, cleared,
                )
                cooldown = set()
                break

    skipped_cooldown = sum(1 for r in rows if r["slug"] in cooldown)
    if skipped_cooldown:
        log.info(
            "%-16s %d/%d boards in cooldown after repeated failures, skipping",
            ats, skipped_cooldown, len(rows),
        )
    rows = [r for r in rows if r["slug"] not in cooldown]

    # Boards that answer fine and never carry a software job — see
    # db.mark_board_yield. Skipped unless `--all-boards` says otherwise, which
    # is what makes the request budget available for the validation backlog.
    quiet = set() if getattr(args, "all_boards", False) else db.quiet_boards(conn, ats)
    skipped_quiet = sum(1 for r in rows if r["slug"] in quiet)
    if skipped_quiet:
        log.info(
            "%-16s %d/%d boards quiet (no software postings for %d+ sweeps), skipping",
            ats, skipped_quiet, len(rows), db.ZERO_YIELD_GRACE,
        )
    rows = [r for r in rows if r["slug"] not in quiet]
    if not rows:
        return empty, FilterFunnel()

    log.info("%-16s fetching %d boards...", ats, len(rows))
    sem = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()
    # `new` and `refreshed` are distinct rows; `kept` counts postings that
    # passed the filters, which can exceed rows when a source repeats a
    # requisition (e.g. one copy per location).
    totals = {"raw": 0, "kept": 0, "new": 0, "refreshed": 0, "boards": 0, "closed": 0,
              "cooled_down": skipped_cooldown, "quiet": skipped_quiet}
    # Why postings were dropped, aggregated across this ATS's boards.
    funnel = FilterFunnel()

    # Lets Ashby/SmartRecruiters/Workday skip detail requests for postings that
    # cannot pass the real filters — the single biggest speedup available.
    allow_roles = _allowed_roles(args)
    relaxed_all = (args.all_roles and args.worldwide
                   and allow_roles >= set(EXCLUDED_ROLE_NAMES))
    prefilter = None if relaxed_all else partial(
        cheap_prefilter, allow_roles=allow_roles
    )

    # Postings already stored skip their detail request. Reading the ids costs
    # one indexed query per board and saves one HTTP round-trip per known
    # posting — on a repeat sweep that is the overwhelming majority of them.
    refetch = getattr(args, "refetch_details", False)

    async def one(row: dict[str, Any]) -> None:
        slug = row["slug"]
        known: set[str] | None = None
        if not refetch:
            async with lock:
                # Stored jobs *plus* postings already evaluated and rejected —
                # the latter are the majority on a repeat sweep.
                known = db.skippable_ids(conn, ats, slug, since, until)
        async with sem:
            try:
                jobs = await source.fetch_board(
                    fetcher, slug, prefilter=prefilter, known_ids=known
                )
            except Exception as exc:
                log.debug("%s/%s failed: %s", ats, slug, exc)
                async with lock:
                    db.mark_board_failure(conn, ats, slug)
                    conn.commit()
                return
        # `None` means the board did not respond — quite different from an empty
        # board, and closures must NOT be inferred from it. Also board_health:
        # a board answering with `None` repeatedly (no exception, just nothing
        # useful) still needs to back off the same as an exception would.
        if jobs is None:
            async with lock:
                db.mark_board_failure(conn, ats, slug)
                conn.commit()
            return

        async with lock:
            db.mark_board_success(conn, ats, slug)
            # A fetch IS the probe `validate` performs, with details on top, so
            # a board that answered with postings is live and should be treated
            # as such from the next sweep on — otherwise the unchecked slice
            # above would have to rediscover it by chance every time.
            #
            # Only the positive verdict is recorded here. An empty answer is
            # ambiguous at this layer: `prefilter` runs inside `fetch_board`, so
            # a board full of forklift-mechanic postings comes back `[]` exactly
            # like a board with nothing on it. Writing "empty" would turn "no
            # SOFTWARE jobs today" into a permanent judgement about the board.
            # `validate` stays the authority for empty/dead.
            if jobs and row.get("status") == "unchecked":
                db.mark_company(conn, ats, slug, "live", len(jobs))
            # The board answered, so anything it no longer lists has closed
            # (after the grace period — see mark_board_closures). Done before
            # filtering: a posting dropped by the date window is still open,
            # and marking it closed would be wrong.
            totals["closed"] += db.mark_board_closures(
                conn, source.name, slug, {j.source_job_id for j in jobs}
            )
            conn.commit()

        if not jobs:
            # An empty answer counts as a zero-yield sweep. It is the branch
            # Paycom takes on all 5,135 of its portals: the prefilter runs
            # inside `fetch_board`, so a board of forklift-mechanic postings
            # arrives here as `[]` having already cost its requests.
            async with lock:
                db.mark_board_yield(conn, ats, slug, 0)
                conn.commit()
            return
        kept, board_funnel = apply_filters_counted(
            jobs, since=since, until=until,
            software_only=not args.all_roles, us_only=not args.worldwide,
            allow_roles=allow_roles,
        )
        # Remember what we looked at and turned away, so the next sweep does
        # not pay for the same detail requests again.
        kept_ids = {j.source_job_id for j in kept}
        rejected = [
            (j.source_job_id, j.posted_at)
            for j in jobs
            if j.source_job_id not in kept_ids and j.description_text
        ]

        async with lock:
            db.mark_board_yield(conn, ats, slug, len(kept))
            if rejected:
                db.record_rejected(conn, ats, slug, rejected)
            funnel.add(board_funnel)
            totals["raw"] += len(jobs)
            totals["boards"] += 1
            if kept:
                inserted, updated = db.upsert_jobs(conn, kept)
                totals["kept"] += len(kept)
                totals["new"] += inserted
                totals["refreshed"] += updated
                # Scoped to this one company, so it's an index range scan, not
                # a table scan — see dedupe.resolve_scoped.
                dedupe.resolve_scoped(conn, slug)
            if totals["boards"] % 100 == 0:
                log.info(
                    "  %s: %d boards, %d raw, %d new",
                    ats, totals["boards"], totals["raw"], totals["new"],
                )

    await asyncio.gather(*(one(row) for row in rows))
    log.info(
        "%-16s done: %d boards, %d postings seen, %d matched, %d new, %d refreshed, %d closed",
        ats, totals["boards"], totals["raw"], totals["kept"],
        totals["new"], totals["refreshed"], totals["closed"],
    )
    if funnel.seen:
        log.info(
            "%-16s filter funnel: seen=%d role=-%d location=-%d window=-%d kept=%d",
            ats, funnel.seen, funnel.dropped_role, funnel.dropped_location,
            funnel.dropped_window, funnel.kept,
        )
    return totals, funnel


async def cmd_fetch(args: argparse.Namespace) -> int:
    since = parse_date(args.since)
    until = parse_date(args.until)
    if since and until and since > until:
        log.error("--since (%s) is after --until (%s)", since, until)
        return 2

    if since:
        age = (date.today() - since).days
        if age > 120:
            log.warning(
                "--since is %d days ago. These APIs only return CURRENTLY OPEN "
                "postings, so jobs from that window that have since closed are "
                "not retrievable from any source.", age,
            )

    selected = set(args.sources) if args.sources else set(ALL_SOURCE_NAMES)
    globals_map = dict(GLOBAL_SOURCES)
    boards_map = dict(BOARD_SOURCES)
    if args.include_restricted:
        log.warning(
            "Restricted sources enabled. These scrape sites whose Terms of Service "
            "prohibit it, and are frequently blocked; results will be unreliable."
        )
        extra_globals = restricted_sources()
        extra_boards = restricted_board_sources()
        globals_map.update(extra_globals)
        boards_map.update(extra_boards)
        if not args.sources:
            selected |= set(extra_globals) | set(extra_boards)

    grand_total = 0
    # Per-source stats persisted with the run so the dashboard can chart them.
    per_source: dict[str, dict[str, Any]] = {}
    with db.session(args.db) as conn:
        # The dashboard opens the run row before spawning us and passes its id
        # through the environment; without this each dashboard-launched run
        # would appear twice in history.
        inherited = os.environ.get("JOBSCRAPE_RUN_ID")
        run_id = int(inherited) if inherited else db.start_run(
            conn, _command_line(), "fetch", pid=os.getpid()
        )
        async with Fetcher(use_cache=not args.no_cache) as fetcher:
            # ATS boards: the coverage backbone.
            for ats in boards_map:
                if ats not in selected:
                    continue
                board_totals, board_funnel = await _fetch_boards(
                    conn, fetcher, ats, since, until, args, boards_map
                )
                grand_total += board_totals["new"] + board_totals["refreshed"]
                per_source[ats] = {**board_totals, **board_funnel.as_dict()}

            # Query-style sources.
            for name, source in globals_map.items():
                if name not in selected:
                    continue
                ok, reason = source.available()
                if not ok:
                    log.info("%-16s skipped — %s", name, reason)
                    continue
                log.info("%-16s fetching...", name)
                try:
                    jobs = await source.fetch(
                        fetcher, since=since, until=until, max_results=args.max_results
                    )
                except Exception as exc:
                    log.warning("%-16s failed: %s", name, exc)
                    continue
                kept_jobs, gfunnel = apply_filters_counted(
                    jobs, since=since, until=until,
                    software_only=not args.all_roles, us_only=not args.worldwide,
                    allow_roles=_allowed_roles(args),
                )
                per_source[name] = gfunnel.as_dict()
                if kept_jobs:
                    inserted, updated = db.upsert_jobs(conn, kept_jobs)
                    grand_total += inserted + updated
                    per_source[name].update(new=inserted, refreshed=updated)
                    log.info(
                        "%-16s %d seen, %d matched (%d new, %d updated)",
                        name, len(jobs), len(kept_jobs), inserted, updated,
                    )
                    dedupe.resolve_many(conn, (j.company_slug for j in kept_jobs))
                else:
                    log.info("%-16s %d seen, 0 matched filters", name, len(jobs))

                # Source-wide closure: a global source's jobs are otherwise
                # unreachable by the per-board sweep, so without this they never
                # close. It is only sound against a crawl that walked its WHOLE
                # result set.
                #
                # Every one of these adapters stops early and returns what it
                # has when a page fails, when a page budget runs out, or when
                # --max-results caps it — so a rate-limited run is
                # indistinguishable from a genuinely shrunken catalogue unless
                # the adapter says which it was. Closing against a truncated
                # "seen" set retires postings the run simply never reached, and
                # the 48h grace hides it until failures repeat, which is exactly
                # when rate-limiting happens. `is_complete` reads a plain list
                # as incomplete, so an adapter that forgets to say under-closes
                # rather than deleting live jobs.
                if not is_complete(jobs):
                    log.info(
                        "%-16s closure skipped — crawl was incomplete "
                        "(capped, budget-limited, or a page failed)", name,
                    )
                elif not jobs:
                    # A run that returned nothing proves the crawl failed, not
                    # that the source is empty.
                    log.info("%-16s closure skipped — returned no postings", name)
                else:
                    closed = db.mark_board_closures(
                        conn, name, None, {j.source_job_id for j in jobs}
                    )
                    if closed:
                        per_source[name]["closed"] = closed
                        log.info("%-16s %d postings closed (unseen > %dh)",
                                  name, closed, config.CLOSURE_GRACE_HOURS)
                    conn.commit()

            log.info("HTTP: %s", dict(fetcher.stats))
            http_stats = dict(fetcher.stats)

        total = db.stats(conn)["total_jobs"]
        db.finish_run(conn, run_id, "ok", {
            "sources": per_source,
            "http": http_stats,
            "rows_touched": grand_total,
            "total_jobs": total,
            "since": str(since) if since else None,
            "until": str(until) if until else None,
        })
    log.info("Fetch complete. %d rows touched; %d total jobs in database.", grand_total, total)
    return 0


# --------------------------------------------------------------------------
# export / stats / sources
# --------------------------------------------------------------------------

def cmd_export(args: argparse.Namespace) -> int:
    with db.session(args.db) as conn:
        rows = db.query_jobs(
            conn,
            since=args.since, until=args.until,
            sources=args.sources, state=args.state,
            company=args.company, search=args.search, limit=args.limit,
        )
        if not rows:
            log.warning("No jobs matched. Run `fetch` first, or widen the filters.")
            return 1
        if getattr(args, "complete_only", False):
            before = len(rows)
            rows = [row for row in rows if export_mod.is_complete(row)]
            # Say what was dropped and why. A row can be missing a field only
            # because the employer never published it, so a silent 60% cut here
            # would read as "the scraper lost most of the jobs".
            log.info("--complete-only: %d of %d rows carry all nine fields (%.1f%%)",
                     len(rows), before, 100 * len(rows) / before)
            if not rows:
                log.warning("Nothing carries all nine fields; widen or drop --complete-only.")
                return 1
        try:
            paths = export_mod.export(
                rows,
                formats=args.format,
                path=Path(args.output) if args.output else None,
            )
        except RuntimeError as exc:
            log.error("%s", exc)
            return 1
    for path in paths:
        log.info("Wrote %d jobs -> %s", len(rows), path)
    return 0 if paths else 1


def cmd_migrate_from_sqlite(args: argparse.Namespace) -> int:
    from . import migrate as migrate_mod

    path = Path(args.sqlite_path)
    try:
        counts = migrate_mod.migrate(path, args.db, dry_run=args.dry_run)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1
    label = "Would migrate" if args.dry_run else "Migrated"
    for table, n in counts.items():
        log.info("%-16s %s %d rows", table, label, n)
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    """Re-apply the current role filter to stored postings and remove failures.

    The archive is written once per posting and never re-evaluated, so a filter
    change only ever affects *future* fetches. This applies it retroactively.

    Deliberately role-only. Region and date are per-run parameters rather than
    archive-wide rules, so re-applying those here would delete rows that a past
    run legitimately kept for a window you asked for at the time.
    """
    allow_roles = _allowed_roles(args)
    with db.session(args.db) as conn:
        rows = conn.execute(
            "SELECT source, source_job_id, title, department, company, company_slug, "
            "posted_at FROM jobs"
        ).fetchall()

        doomed = [
            r for r in rows
            if not apply_filters_mod.is_software_role(
                r["title"], r["department"], allow_roles=allow_roles,
            )
        ]
        if not doomed:
            log.info("Nothing to purge: all %d stored postings still pass the role filter.",
                     len(rows))
            return 0

        # Show what is going, grouped, before anything is destroyed.
        # `is_excluded_role` returns the category, so this needs no re-testing.
        by_reason: collections.Counter[str] = collections.Counter()
        for r in doomed:
            by_reason[
                apply_filters_mod.is_excluded_role(r["title"], allow_roles) or "other"
            ] += 1

        log.info("%s %d of %d stored postings (%.1f%%)",
                 "Would purge" if args.dry_run else "Purging",
                 len(doomed), len(rows), 100 * len(doomed) / len(rows))
        for reason, count in by_reason.most_common():
            if count:
                log.info("    %-12s %6d", reason, count)
        if args.sample:
            for r in doomed[:args.sample]:
                log.info("    [%s] %s -- %s", r["source"], r["company"], r["title"])

        if args.dry_run:
            log.info("Dry run: nothing deleted. Re-run without --dry-run to apply.")
            return 0

        # Record before deleting, so the next sweep skips these instead of
        # re-downloading and re-rejecting them. This is what posting_seen is for.
        # Keyed on company_SLUG, not the display name. `record_rejected`'s third
        # argument is `company_slug` and `skippable_ids` reads it back against
        # `companies.slug`, so writing "Anduril Industries" where `anduril`
        # belongs means the lookup never matches and the promise this block
        # makes — "recorded as rejected so they are not re-fetched" — is void.
        # 8,524 rows in the archive were written that way before this was fixed.
        by_source: dict[tuple[str, str], list[tuple[str, str | None]]] = {}
        for r in doomed:
            key = (r["source"], r["company_slug"] or "")
            by_source.setdefault(key, []).append((r["source_job_id"], r["posted_at"]))
        for (source, company_slug), entries in by_source.items():
            db.record_rejected(conn, source, company_slug, entries)

        deleted, released = db.delete_jobs(
            conn, [(r["source"], r["source_job_id"]) for r in doomed]
        )

        log.info("Deleted %d posting(s); %d recorded as rejected so they are not re-fetched",
                 deleted, len(doomed))
        if released:
            log.info(
                "Released %d duplicate(s) whose canonical was purged — they were "
                "suppressed by a row that no longer exists. Run "
                "`python -m jobscrape dedupe --backfill` to re-cluster them.",
                released,
            )
    return 0


def cmd_dedupe(args: argparse.Namespace) -> int:
    with db.session(args.db) as conn:
        if args.clear is not None:
            db.clear_duplicate(conn, args.clear)
            log.info("Cleared duplicate flag on job id %d", args.clear)
            return 0
        result = dedupe.resolve_all(conn, dry_run=args.dry_run)
        label = "Would collapse" if args.dry_run else "Collapsed"
        log.info(
            "%s %d of %d open jobs (tier1: %d clusters / %d rows, tier2: %d clusters / %d rows)",
            label, result["collapsed"], result["candidates"],
            result["tier1_clusters"], result["tier1_rows"],
            result["tier2_clusters"], result["tier2_rows"],
        )
        if args.sample:
            for cluster in dedupe.sample_clusters(conn, limit=args.sample):
                log.info("cluster canonical=%d:", cluster["canonical_id"])
                for m in cluster["members"]:
                    marker = " (canonical)" if m["duplicate_of"] is None else ""
                    log.info("  [%s] %s -- %s%s", m["source"], m["company"], m["title"], marker)
    return 0


def cmd_import_tenants(args: argparse.Namespace) -> int:
    """Seed companies from a published ATS tenant list.

    Imported as `unchecked`; the `validate` stage decides what is real. Slugs
    already known are left alone, so a re-import never resets a validated board.
    """
    from .discovery import tenant_lists

    directory = Path(args.directory)
    if not directory.is_dir():
        log.error("not a directory: %s", directory)
        return 1

    if args.dry_run:
        total = skipped = 0
        for platform, ats, slugs, missed in tenant_lists.iter_lists(directory):
            total += len(slugs)
            skipped += missed
            log.info("%-16s %6d slugs, %4d unparseable", platform, len(slugs), missed)
        log.info("Would import %d slug(s); %d unparseable", total, skipped)
        return 0

    with db.session(args.db) as conn:
        added = tenant_lists.import_directory(conn, directory)
        conn.commit()
    log.info("Imported %d new company slug(s) across %d platform(s)",
             sum(added.values()), len(added))
    return 0


async def cmd_liveness(args: argparse.Namespace) -> int:
    """Probe postings the fetch sweep can never close, and close the dead ones."""
    from . import liveness

    sources = args.sources or list(config.LIVENESS_SOURCES)
    counts = {liveness.LIVE: 0, liveness.EXPIRED: 0, liveness.UNCERTAIN: 0}
    closed = skipped = 0

    with db.session(args.db) as conn:
        candidates = db.liveness_candidates(conn, sources, limit=args.limit)
        if not candidates:
            log.info("liveness: nothing to probe for %s", ", ".join(sources) or "(none)")
            return 0

        probable = [c for c in candidates if liveness.is_probable(c["job_url"])]
        skipped = len(candidates) - len(probable)
        log.info(
            "liveness: probing %d postings (%d skipped — their URL outlives the vacancy)",
            len(probable), skipped,
        )

        sem = asyncio.Semaphore(args.concurrency)
        lock = asyncio.Lock()

        async with Fetcher(use_cache=False) as fetcher:
            async def one(row: dict[str, Any]) -> None:
                nonlocal closed
                async with sem:
                    probe = await liveness.probe_url(fetcher, row["job_url"])
                strikes, should_close = liveness.apply_verdict(
                    row["liveness_strikes"] or 0, probe.verdict
                )
                async with lock:
                    counts[probe.verdict] += 1
                    if args.dry_run:
                        return
                    db.record_liveness(conn, row["id"], strikes, should_close)
                    if should_close:
                        closed += 1
                        log.info(
                            "  closed #%s (%s) — %s", row["id"], row["source"], probe.reason
                        )
                    conn.commit()

            await asyncio.gather(*(one(row) for row in probable))

    label = "would close" if args.dry_run else "closed"
    log.info(
        "liveness: %d live, %d expired, %d uncertain; %s %d "
        "(a posting closes at %d consecutive expired reads)",
        counts[liveness.LIVE], counts[liveness.EXPIRED], counts[liveness.UNCERTAIN],
        label, closed, liveness.CLOSE_THRESHOLD,
    )
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Launch the local web dashboard."""
    try:
        from .dashboard import serve
    except ImportError as exc:
        log.error("The dashboard needs fastapi, uvicorn and jinja2: %s", exc)
        log.error("Install with: pip install fastapi uvicorn jinja2")
        return 1
    log.info("Dashboard on http://%s:%d  (Ctrl-C to stop)", args.host, args.port)
    serve(host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_webhook(args: argparse.Namespace) -> int:
    """Serve the Apify webhook receiver.

    Separate from `dashboard` on purpose: this one is meant to be publicly
    reachable, and the dashboard has no authentication on any route.
    """
    try:
        from .webhook import serve
    except ImportError as exc:
        log.error("The webhook receiver needs fastapi and uvicorn: %s", exc)
        log.error("Install with: pip install fastapi uvicorn")
        return 1
    log.info("Apify webhook on http://%s:%d/apify  (Ctrl-C to stop)",
             args.host, args.port)
    serve(host=args.host, port=args.port)
    return 0


async def cmd_apify_pull(args: argparse.Namespace) -> int:
    """Ingest Apify runs by hand.

    The recovery path. A webhook delivery lost while this was down is otherwise
    gone — Apify gives up after 11 retries — and this is also how to backfill
    runs from before the receiver existed, or to test the mapping end to end
    without waiting for a scheduled run.
    """
    from .http import Fetcher
    from .sources.apify import client
    from .sources.apify import ingest as ingest_module

    ok, reason = client.available()
    if not ok:
        log.error("%s", reason)
        return 1

    run_ids: list[str] = list(args.run or [])
    if not run_ids:
        actors = args.actor or [config.APIFY_INDEED_ACTOR, config.APIFY_LINKEDIN_ACTOR]
        async with Fetcher() as fetcher:
            for actor in actors:
                runs = await client.last_runs(fetcher, actor, limit=args.last)
                if not runs:
                    log.info("%-28s no succeeded runs found", actor)
                for run in runs:
                    if run.get("id"):
                        run_ids.append(str(run["id"]))
    if not run_ids:
        log.error("Nothing to ingest — no runs matched.")
        return 1

    failures = 0
    with db.session(args.db) as conn:
        for run_id in run_ids:
            result = await ingest_module.ingest_and_record(
                conn, run_id, command="apify-pull",
                max_items=args.max_results,
                allow_roles=_allowed_roles(args),
                software_only=not args.all_roles,
                us_only=not args.worldwide,
            )
            if result.error:
                failures += 1
            if result.funnel:
                log.info("   funnel: %s", result.funnel)
    return 1 if failures and failures == len(run_ids) else 0


def cmd_stats(args: argparse.Namespace) -> int:
    with db.session(args.db) as conn:
        print(json.dumps(db.stats(conn), indent=2))
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    print(f"{'SOURCE':<18} {'TYPE':<8} {'READY':<6} NOTE")
    print("-" * 100)
    for ats, source in BOARD_SOURCES.items():
        ok, reason = source.available()
        print(f"{ats:<18} {'board':<8} {'yes' if ok else 'no':<6} {source.note or reason}")
    for name, source in GLOBAL_SOURCES.items():
        ok, reason = source.available()
        print(f"{name:<18} {'global':<8} {'yes' if ok else 'no':<6} {source.note}")
        if not ok:
            print(f"{'':<34} -> {reason}")
    print()
    for kind, registry in (("global", restricted_sources), ("board", restricted_board_sources)):
        for name, source in registry().items():
            print(f"{name:<18} {kind:<8} {'opt-in':<6} {source.note}")
    print("\nOpt-in sources require --include-restricted and are off by default.")
    return 0


# --------------------------------------------------------------------------
# run: the one-command path
# --------------------------------------------------------------------------

async def cmd_run(args: argparse.Namespace) -> int:
    """Discover (if needed) -> validate (if needed) -> fetch -> export."""
    with db.session(args.db) as conn:
        known = sum(
            r["c"] for r in conn.execute("SELECT COUNT(*) c FROM companies")
        )
    if known < 100:
        log.info("No company registry yet — running discovery first.")
        rc = await cmd_discover(args)
        if rc:
            return rc

    with db.session(args.db) as conn:
        unchecked = conn.execute(
            "SELECT COUNT(*) c FROM companies WHERE status='unchecked'"
        ).fetchone()["c"]
    if unchecked and not args.skip_validate:
        log.info("Validating %d unchecked boards...", unchecked)
        # `run`'s --limit belongs to export; validation has its own cap so the
        # two cannot interfere.
        validate_args = argparse.Namespace(**vars(args))
        validate_args.limit = args.validate_limit
        # Don't probe boards this run will never fetch from.
        if not args.ats and args.sources:
            scoped = [a for a in BOARD_SOURCES if a in set(args.sources)]
            validate_args.ats = scoped or None
        await cmd_validate(validate_args)

    rc = await cmd_fetch(args)
    if rc:
        return rc
    return cmd_export(args)


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobscrape",
        description="Collect US software job postings from ATS boards and job APIs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical use:\n"
            "  python -m jobscrape run --since 2026-06-01 --until 2026-07-28\n\n"
            "Or step by step:\n"
            "  python -m jobscrape discover\n"
            "  python -m jobscrape validate\n"
            "  python -m jobscrape fetch --since 2026-07-01\n"
            "  python -m jobscrape export --format csv --format md\n\n"
            "Note: these APIs return only CURRENTLY OPEN postings. Jobs from a past\n"
            "window that have since closed cannot be retrieved by any source."
        ),
    )
    # Accept -v/--db either before or after the subcommand. The subparser copies
    # use SUPPRESS so an omitted flag doesn't clobber the top-level value.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true",
                        default=argparse.SUPPRESS)
    common.add_argument("--db", default=argparse.SUPPRESS,
                        help="database URL override (default: $DATABASE_URL)")

    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--db", default=None,
                        help="database URL override (default: $DATABASE_URL)")
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=partial(argparse.ArgumentParser, parents=[common]),
    )

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--concurrency", type=int, default=8)
        p.add_argument("--no-cache", action="store_true", help="bypass the disk cache")

    def add_window(p: argparse.ArgumentParser) -> None:
        p.add_argument("--since", help="earliest posting date (YYYY-MM-DD)")
        p.add_argument("--until", help="latest posting date (YYYY-MM-DD)")

    def add_filters(p: argparse.ArgumentParser) -> None:
        p.add_argument("--all-roles", action="store_true",
                       help="keep non-software roles too")
        p.add_argument("--worldwide", action="store_true",
                       help="keep non-US locations too")
        p.add_argument("--include-roles", action="append", choices=EXCLUDED_ROLE_NAMES,
                       metavar="CATEGORY",
                       help="re-admit an excluded role category (repeatable). One of: "
                            + ", ".join(EXCLUDED_ROLE_NAMES) + ". These are all genuine "
                            "software roles held out by preference. Note this does not "
                            "resurrect postings already turned away: those are recorded "
                            "in posting_seen and skipped, so pair it with "
                            "--refetch-details to re-evaluate them.")
        # Shorthand that predates --include-roles and is documented in the
        # README, so kept working rather than broken.
        p.add_argument("--include-qa", action="store_true",
                       help="alias for --include-roles qa")
        p.add_argument("--include-management", action="store_true",
                       help="alias for --include-roles management")

    # discover
    p_disc = sub.add_parser("discover", help="harvest ATS company slugs from Common Crawl")
    p_disc.add_argument("--collections", type=int, default=config.CC_COLLECTIONS,
                        help="how many recent crawl collections to read")
    p_disc.add_argument("--max-pages", type=int, default=None,
                        help="cap index pages per host (for a quick sample)")
    p_disc.add_argument("--no-workday", action="store_true")
    p_disc.add_argument("--vector", action="append", choices=["cc", "ct", "github"],
                        help="discovery vectors to run (default: all three)")
    add_common(p_disc)

    # validate
    p_val = sub.add_parser("validate", help="probe discovered slugs, discard junk")
    p_val.add_argument("--ats", action="append", choices=_ATS_CHOICES)
    p_val.add_argument("--limit", type=int, default=None)
    p_val.add_argument("--recheck", action="store_true", help="re-probe all, not just unchecked")
    p_val.add_argument("--sample", action="store_true",
                       help="draw the --limit slice at random, so the live-rate it "
                            "reports describes the platform rather than the letter A")
    add_common(p_val)

    # fetch
    p_fetch = sub.add_parser("fetch", help="pull postings into the database")
    add_window(p_fetch)
    add_filters(p_fetch)
    add_common(p_fetch)
    p_fetch.add_argument("--sources", action="append",
                         help="restrict to named sources (repeatable)")
    p_fetch.add_argument("--companies", type=int, default=None,
                         help="max boards per ATS")
    p_fetch.add_argument("--unchecked", type=int, default=UNCHECKED_PER_ATS,
                         help="how many not-yet-validated boards per ATS to include "
                              f"(default {UNCHECKED_PER_ATS}; 0 = live boards only)")
    p_fetch.add_argument("--all-boards", action="store_true",
                         help="also sweep boards backed off for yielding no software "
                              "postings (see zero_yield_streak); slow, and mostly "
                              "useful right after loosening the role filter")
    p_fetch.add_argument("--max-results", type=int, default=None,
                         help="cap results per global source")
    p_fetch.add_argument("--include-restricted", action="store_true",
                         help="enable LinkedIn/Indeed scrapers (violates their ToS)")
    p_fetch.add_argument("--refetch-details", action="store_true",
                         help="re-download descriptions for postings already stored "
                              "(slow; only needed to repair existing rows)")

    # export
    p_exp = sub.add_parser("export", help="write collected jobs to a file")
    add_window(p_exp)
    p_exp.add_argument("--format", action="append",
                       choices=["csv", "jsonl", "xlsx", "md"], default=None)
    p_exp.add_argument("--output", help="output path (extension set per format)")
    p_exp.add_argument("--sources", action="append")
    p_exp.add_argument("--state", help="two-letter US state code")
    p_exp.add_argument("--company")
    p_exp.add_argument("--search", help="substring in title or description")
    p_exp.add_argument("--limit", type=int, default=None)
    p_exp.add_argument("--complete-only", action="store_true",
                       help="keep only postings carrying all nine fields (title, source, "
                            "location, mode, seniority, salary, skills, description, link). "
                            "Most postings do not state a salary or work mode, so expect "
                            "this to cut the result set substantially.")

    # run
    p_run = sub.add_parser("run", help="discover + validate + fetch + export in one go")
    add_window(p_run)
    add_filters(p_run)
    add_common(p_run)
    p_run.add_argument("--sources", action="append")
    p_run.add_argument("--companies", type=int, default=None)
    p_run.add_argument("--unchecked", type=int, default=UNCHECKED_PER_ATS)
    p_run.add_argument("--all-boards", action="store_true")
    p_run.add_argument("--max-results", type=int, default=None)
    p_run.add_argument("--include-restricted", action="store_true")
    p_run.add_argument("--refetch-details", action="store_true")
    p_run.add_argument("--skip-validate", action="store_true")
    p_run.add_argument("--validate-limit", type=int, default=None,
                       help="cap boards probed during the validate step")
    p_run.add_argument("--collections", type=int, default=config.CC_COLLECTIONS)
    p_run.add_argument("--max-pages", type=int, default=None)
    p_run.add_argument("--no-workday", action="store_true")
    p_run.add_argument("--vector", action="append", choices=["cc", "ct", "github"])
    p_run.add_argument("--format", action="append",
                       choices=["csv", "jsonl", "xlsx", "md"], default=None)
    p_run.add_argument("--output")
    p_run.add_argument("--state")
    p_run.add_argument("--company")
    p_run.add_argument("--search")
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--ats", action="append", choices=_ATS_CHOICES)
    p_run.add_argument("--recheck", action="store_true")

    p_dash = sub.add_parser("dashboard", help="launch the local web dashboard")
    p_dash.add_argument("--host", default="127.0.0.1")
    p_dash.add_argument("--port", type=int, default=8000)
    p_dash.add_argument("--reload", action="store_true", help="auto-reload on edit")

    p_hook = sub.add_parser(
        "webhook",
        help="serve the Apify webhook receiver (public; needs APIFY_WEBHOOK_SECRET)",
    )
    p_hook.add_argument("--host", default="0.0.0.0")
    p_hook.add_argument("--port", type=int, default=8001)

    p_apify = sub.add_parser(
        "apify-pull",
        help="ingest Apify runs by hand — recovery for a missed webhook, or a backfill",
    )
    p_apify.add_argument("--run", action="append",
                         help="ingest this run id (repeatable)")
    p_apify.add_argument("--actor", action="append",
                         help="actor to read recent runs from (repeatable); "
                              "defaults to the configured Indeed and LinkedIn actors")
    p_apify.add_argument("--last", type=int, default=1,
                         help="how many recent succeeded runs per actor (default 1)")
    p_apify.add_argument("--max-results", type=int, default=None,
                         help="cap items read per run")
    p_apify.add_argument("--all-roles", action="store_true",
                         help="keep non-software roles too")
    p_apify.add_argument("--worldwide", action="store_true",
                         help="keep non-US locations too")
    p_apify.add_argument("--include-roles", action="append", metavar="CATEGORY",
                         choices=EXCLUDED_ROLE_NAMES, default=None,
                         help="re-admit an excluded role category (repeatable)")

    sub.add_parser("stats", help="summarize what has been collected")
    sub.add_parser("sources", help="list every source and whether it is ready")

    # migrate-from-sqlite
    p_mig = sub.add_parser(
        "migrate-from-sqlite",
        help="one-time copy of the legacy SQLite archive into Postgres",
    )
    p_mig.add_argument("--sqlite-path", default=str(config.DB_PATH),
                       help="path to the legacy jobs.db")
    p_mig.add_argument("--dry-run", action="store_true",
                       help="report row counts without writing")

    # liveness
    p_live = sub.add_parser(
        "liveness",
        help="probe postings the fetch sweep can't reach; close the dead ones",
    )
    p_live.add_argument("--sources", action="append",
                        help="override which sources to probe (default: sampling sources)")
    p_live.add_argument("--limit", type=int, default=500,
                        help="max postings to probe this run")
    p_live.add_argument("--concurrency", type=int, default=6,
                        help="keeps the probe from hammering any one employer site")
    p_live.add_argument("--dry-run", action="store_true",
                        help="report verdicts without writing")

    # dedupe
    p_tenants = sub.add_parser(
        "import-tenants",
        help="seed companies from a published ATS tenant list (MIT-licensed only)",
    )
    p_tenants.add_argument("directory", help="directory of <platform>.csv files")
    p_tenants.add_argument("--dry-run", action="store_true",
                           help="report what would be imported without writing")

    p_purge = sub.add_parser(
        "purge",
        help="delete stored postings that no longer pass the role filter",
    )
    p_purge.add_argument("--dry-run", action="store_true",
                         help="report what would be deleted without writing")
    p_purge.add_argument("--sample", type=int, default=0, metavar="N",
                         help="print N of the postings that would go")
    p_purge.add_argument("--include-roles", action="append", choices=EXCLUDED_ROLE_NAMES,
                         metavar="CATEGORY",
                         help="spare a role category from the purge (repeatable). One of: "
                              + ", ".join(EXCLUDED_ROLE_NAMES))
    p_purge.add_argument("--include-qa", action="store_true",
                         help="alias for --include-roles qa")
    p_purge.add_argument("--include-management", action="store_true",
                         help="alias for --include-roles management")

    p_dedupe = sub.add_parser("dedupe", help="collapse reposts and cross-source duplicates")
    p_dedupe.add_argument("--backfill", action="store_true",
                          help="recompute clusters across the whole archive (the only mode; "
                               "this flag exists so the intent is explicit at the call site)")
    p_dedupe.add_argument("--dry-run", action="store_true",
                          help="report what would collapse without writing")
    p_dedupe.add_argument("--sample", type=int, default=0, metavar="N",
                          help="print N resolved clusters for review")
    p_dedupe.add_argument("--clear", type=int, default=None, metavar="JOB_ID",
                          help="un-suppress one job by id (reverses a mis-cluster)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    config.ensure_dirs()

    # argparse `append` defaults can't express "csv unless overridden".
    if getattr(args, "format", None) is None and args.command in ("export", "run"):
        args.format = ["csv"]
    # `validate` reuses --limit; `run` needs it for export only.
    if args.command == "run" and not hasattr(args, "limit"):
        args.limit = None

    try:
        if args.command == "discover":
            return asyncio.run(cmd_discover(args))
        if args.command == "validate":
            return asyncio.run(cmd_validate(args))
        if args.command == "fetch":
            return asyncio.run(cmd_fetch(args))
        if args.command == "run":
            return asyncio.run(cmd_run(args))
        if args.command == "export":
            return cmd_export(args)
        if args.command == "stats":
            return cmd_stats(args)
        if args.command == "sources":
            return cmd_sources(args)
        if args.command == "dashboard":
            return cmd_dashboard(args)
        if args.command == "webhook":
            return cmd_webhook(args)
        if args.command == "apify-pull":
            return asyncio.run(cmd_apify_pull(args))
        if args.command == "migrate-from-sqlite":
            return cmd_migrate_from_sqlite(args)
        if args.command == "dedupe":
            return cmd_dedupe(args)
        if args.command == "import-tenants":
            return cmd_import_tenants(args)
        if args.command == "purge":
            return cmd_purge(args)
        if args.command == "liveness":
            return asyncio.run(cmd_liveness(args))
    except KeyboardInterrupt:
        log.warning("Interrupted — progress is saved; re-run to resume.")
        return 130
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
