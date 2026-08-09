"""Push new/updated/closed postings to a separate Render-hosted Postgres
database that a website reads from directly.

This is an output target, not an input `Source` — it never fetches
anything, it only pushes jobscrape's own already-collected archive out. See
`jobscrape/sources/` for the input side.

The two databases have unrelated schemas: Render's `jobs` table is the
website's own, with its own dedup rule (`dedup_key`, replicated below
exactly) and no notion of "closed" at all — it's meant to hold currently
open listings only. `_to_render_row` is the one place that translation
happens; everything else here is watermark bookkeeping and plain upserts.
"""

from __future__ import annotations

import functools
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from . import config, db

log = logging.getLogger("jobscrape.render_sync")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_CHANGED_SINCE_CTE = """
    WITH changed AS (
        SELECT source, source_job_id, title, company, location_raw,
               salary_min, salary_max, apply_url, job_url, description_text,
               posted_at::date AS posted_date, first_seen_at, skills,
               department, seniority, closed_at, duplicate_of,
               GREATEST(last_seen_at, COALESCE(closed_at, last_seen_at),
                        COALESCE(duplicate_marked_at, last_seen_at)) AS changed_at
        FROM jobs
        WHERE last_seen_at > ? OR closed_at > ? OR duplicate_marked_at > ?
    )
"""
#: Deliberately NOT `AND duplicate_of IS NULL`: a row that just became a
#: duplicate needs exactly one more pass through here to get deleted from
#: Render (see `sync`'s loop) -- filtering it out here would hide the one
#: event that has to reach that DELETE. `duplicate_of IS NOT NULL` rows are
#: still excluded from ever being freshly upserted (the loop below checks
#: it), so a long-suppressed duplicate whose `duplicate_marked_at` predates
#: the watermark simply never resurfaces, same as before.

_CHANGED_SINCE_SQL = _CHANGED_SINCE_CTE + "SELECT * FROM changed ORDER BY changed_at ASC"
#: Oldest-changed-first, so a `--limit`-capped run and the eventual full run
#: behind it cover a contiguous range rather than an arbitrary, unordered
#: slice — repeatable across several capped test runs in a row.

_TIE_SPILLOVER_SQL = _CHANGED_SINCE_CTE + "SELECT * FROM changed WHERE changed_at = ?"
#: `last_seen_at`/`closed_at`/`duplicate_marked_at` are each stamped ONCE per
#: batch call (`upsert_jobs`, `mark_board_closures`, `dedupe._apply` each
#: compute one timestamp and apply it across every row they touch), so many
#: rows sharing the exact same `changed_at` is the common case, not a rare
#: edge. A `LIMIT`-capped fetch ordered by `changed_at ASC` can land its cut
#: in the middle of such a group -- and because the next run's watermark
#: comparison is strict (`changed_at > ?`), any sibling left behind at that
#: exact boundary value would never be selected again by any future run,
#: capped or not. This query pulls in the rest of that boundary tie so the
#: watermark this pass computes only ever advances past a value it fully
#: consumed — see the call site in `sync`.

_UPSERT_SQL = """
    INSERT INTO jobs (
        external_job_id, source_platform, dedup_key, title, company,
        location, salary_min, salary_max, jd_url, jd_text, posted_date,
        created_at, skills, domain, level
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (dedup_key) DO UPDATE SET
        external_job_id = excluded.external_job_id,
        source_platform = excluded.source_platform,
        title = excluded.title,
        company = excluded.company,
        location = excluded.location,
        salary_min = excluded.salary_min,
        salary_max = excluded.salary_max,
        jd_url = excluded.jd_url,
        jd_text = excluded.jd_text,
        posted_date = excluded.posted_date,
        skills = excluded.skills,
        domain = excluded.domain,
        level = excluded.level
"""
#: `created_at` is deliberately absent from the UPDATE list above — set once
#: on first insert, never overwritten on a refresh, the same "don't clobber
#: history" rule jobscrape's own `db.upsert_jobs` applies to `first_seen_at`.

_DELETE_SQL = "DELETE FROM jobs WHERE dedup_key = ?"

_BATCH_SIZE = 500
#: Round-trip latency to Render's Postgres (measured ~220ms) dominates this
#: module's wall-clock time far more than query execution ever does -- one
#: statement per row turns tens of thousands of changed rows into hours,
#: and holds one open transaction (and Render's table lock) the whole time.
#: Batching into multi-row statements cuts the round-trip count by roughly
#: this factor, and flushing (committing) each batch as it goes means a
#: closed/duplicate posting stops lingering on the site for hours after it
#: should have disappeared -- and a crash mid-run only loses the one batch
#: still in flight, not everything since the last watermark. 500 rows * 15
#: columns = 7,500 bound params per upsert batch, comfortably under
#: Postgres's ~65,535-parameter ceiling per statement.


def _batch_upsert_sql(n: int) -> str:
    row = "(" + ", ".join(["?"] * 15) + ")"
    return f"""
        INSERT INTO jobs (
            external_job_id, source_platform, dedup_key, title, company,
            location, salary_min, salary_max, jd_url, jd_text, posted_date,
            created_at, skills, domain, level
        ) VALUES {", ".join([row] * n)}
        ON CONFLICT (dedup_key) DO UPDATE SET
            external_job_id = excluded.external_job_id,
            source_platform = excluded.source_platform,
            title = excluded.title,
            company = excluded.company,
            location = excluded.location,
            salary_min = excluded.salary_min,
            salary_max = excluded.salary_max,
            jd_url = excluded.jd_url,
            jd_text = excluded.jd_text,
            posted_date = excluded.posted_date,
            skills = excluded.skills,
            domain = excluded.domain,
            level = excluded.level
    """


def _batch_delete_sql(n: int) -> str:
    return f"DELETE FROM jobs WHERE dedup_key IN ({', '.join(['?'] * n)})"

_WATERMARK_SQL = """
    SELECT stats->>'watermark' AS watermark FROM runs
    WHERE stage = 'render-sync' AND status = 'ok'
      AND stats->>'watermark' IS NOT NULL
      AND stats->>'dry_run' IS DISTINCT FROM 'true'
    ORDER BY id DESC LIMIT 1
"""

#: Arbitrary constant naming this module's slice of Postgres's single,
#: database-wide advisory-lock keyspace — picked by hand so it stays
#: readable here rather than being the opaque output of some hash.
_LOCK_KEY = 913_427_730_162


class RenderSyncLocked(Exception):
    """Another `sync()`/`backfill_local_sync_status()`/`delete_source()` call
    already holds the lock — see `_locked()`."""


def _try_lock(conn: db.Connection) -> bool:
    return bool(
        conn.execute("SELECT pg_try_advisory_lock(?) AS ok", (_LOCK_KEY,)).fetchone()["ok"]
    )


def _unlock(conn: db.Connection) -> None:
    conn.execute("SELECT pg_advisory_unlock(?)", (_LOCK_KEY,))


@contextmanager
def _locked(conn: db.Connection):
    """Serializes `sync()`, `backfill_local_sync_status()`, and
    `delete_source()` against each other via a session-scoped Postgres
    advisory lock on the LOCAL database — the one all three read and mutate
    overlapping state against (jobscrape's local `jobs` rows, Render's own
    `jobs` rows, and, for `sync()`, the watermark computed from
    `runs.stats`).

    Two of these running at once can race in a way that resurrects exactly
    what the other was trying to remove: `delete_source` computing a
    dedup_key from a row's own data right before deleting it locally, while
    a concurrent `sync()` pass reads that same still-there row as "changed"
    and re-upserts it to Render moments later. The dashboard's own
    `RunManager` already keeps two dashboard-LAUNCHED `render-sync` stages
    from overlapping (see `runner.STAGE_GROUPS`), but `delete_source` and
    `backfill_local_sync_status` are CLI-only, and even a plain
    `python -m jobscrape render-sync` invoked twice bypasses that
    reservation entirely — this is the guarantee that holds regardless of
    how any of the three was launched.

    `pg_try_advisory_lock` (non-blocking), not the blocking form: a second
    caller skipping outright with a clear message is safer than silently
    queuing for however long the first one takes — especially from a cron
    context, where a queued second invocation could still be running when a
    third one fires.
    """
    if not _try_lock(conn):
        raise RenderSyncLocked(
            "another render-sync/backfill/delete-source is already running"
        )
    try:
        yield
    finally:
        _unlock(conn)


def _serialized(func):
    """Wraps a `(conn, render_conn, ...)` function in `_locked(conn)` without
    touching its body — see `_locked`'s docstring for why this exists."""
    @functools.wraps(func)
    def wrapper(conn: db.Connection, *args: Any, **kwargs: Any) -> Any:
        with _locked(conn):
            return func(conn, *args, **kwargs)
    return wrapper


def available() -> tuple[bool, str]:
    if not config.RENDER_DATABASE_URL:
        return False, "set RENDER_DATABASE_URL"
    return True, ""


def connect_render() -> db.Connection:
    """A connection to the *separate* Render database — never via
    `db.connect()`. That function unconditionally runs `ensure_schema()`,
    which would try to create jobscrape's own `jobs`/`companies`/... tables
    against a database that already has its own, completely different
    `jobs` table. Render's schema is never jobscrape's to manage.
    """
    engine = create_engine(config.RENDER_DATABASE_URL)
    return db.Connection(engine.connect())


def _dedup_key(source_platform: str, external_job_id: str | None,
                jd_url: str | None, title: str, company: str) -> str:
    """The website's own rule, replicated exactly:

        externalJobId present -> f"{sourcePlatform}:{externalJobId}"
        else jdUrl present     -> jdUrl
        else                   -> f"{sourcePlatform}:{title}:{company}"

    jobscrape's `source_job_id` is a required `Job` field, so in practice
    the first branch fires almost always — the fallbacks exist so a row
    that somehow lacks one still keys the same way the website's own
    inserts would.
    """
    if external_job_id:
        return f"{source_platform}:{external_job_id}"
    if jd_url:
        return jd_url
    return f"{source_platform}:{title}:{company}"


def _skills_array(skills_text: str | None) -> list[str]:
    """jobscrape stores this comma-joined in one TEXT column; Render wants
    a real `text[]`."""
    if not skills_text:
        return []
    return [s.strip() for s in skills_text.split(",") if s.strip()]


def _to_render_row(row: dict[str, Any]) -> dict[str, Any]:
    """One jobscrape `jobs` row -> the shape Render's `jobs` table expects."""
    source_platform = row.get("source") or ""
    external_job_id = row.get("source_job_id")
    jd_url = row.get("apply_url") or row.get("job_url")
    salary_min = row.get("salary_min")
    salary_max = row.get("salary_max")
    return {
        "external_job_id": external_job_id,
        "source_platform": source_platform,
        "dedup_key": _dedup_key(
            source_platform, external_job_id, jd_url,
            row.get("title") or "", row.get("company") or "",
        ),
        "title": row.get("title"),
        "company": row.get("company"),
        "location": row.get("location_raw"),
        "salary_min": int(round(salary_min)) if salary_min is not None else None,
        "salary_max": int(round(salary_max)) if salary_max is not None else None,
        "jd_url": jd_url,
        "jd_text": row.get("description_text"),
        "posted_date": row.get("posted_date"),
        "created_at": row.get("first_seen_at"),
        "skills": _skills_array(row.get("skills")),
        "domain": row.get("department"),
        "level": row.get("seniority"),
    }


@_serialized
def sync(
    conn: db.Connection,
    render_conn: db.Connection,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """One incremental pass: push jobscrape rows changed since the last
    successful sync into Render's `jobs` table, removing ones that closed
    or that jobscrape's own dedupe just suppressed.

    "Changed" watches `last_seen_at` (bumped on every re-observation),
    `closed_at` (set by its own UPDATE, `db.py`'s `mark_board_closures`/
    liveness paths — *without* touching `last_seen_at`), and
    `duplicate_marked_at` (set by `dedupe._apply`/`db.clear_duplicate`/
    `db.delete_jobs` whenever `duplicate_of` transitions, either direction —
    also without touching `last_seen_at`) — watching only `last_seen_at`
    would silently never notice a closure or a dedupe suppression. A row
    with `duplicate_of` set is deleted from Render, same as a closed one: a
    repost shouldn't reach the website as a second listing, and unlike the
    old `duplicate_of IS NULL` filter this still lets the transition ITSELF
    through so the row's already-synced Render copy actually gets removed
    instead of silently orphaned.

    The new watermark is the newest `changed_at` actually observed in this
    batch, not wall-clock "now" — a slow concurrent write mid-flight just
    gets picked up on the next pass instead of being silently skipped.

    `limit`, unlike `dry_run`, still writes for real — it's a cap on how
    many rows one pass processes (e.g. to test against a handful before a
    full backfill), not a preview. The watermark advances to cover exactly
    what was actually processed, so a later uncapped run picks up right
    after rather than redoing this batch. It can process a few more than
    `limit` rows when the cut would otherwise land inside a tie of equal
    `changed_at` values — see `_TIE_SPILLOVER_SQL`.
    """
    row = conn.execute(_WATERMARK_SQL).fetchone()
    watermark: datetime = (
        datetime.fromisoformat(row["watermark"]) if row and row["watermark"] else _EPOCH
    )

    sql, params = _CHANGED_SINCE_SQL, [watermark, watermark, watermark]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    changed = conn.execute(sql, tuple(params)).fetchall()

    if changed and limit is not None and len(changed) == limit:
        # The batch came back exactly full, so the cut may have landed
        # inside a tie rather than at a natural end -- pull in the rest of
        # the boundary group so the watermark never advances past a
        # sibling this pass left unprocessed.
        boundary = changed[-1]["changed_at"]
        spillover = conn.execute(
            _TIE_SPILLOVER_SQL, (watermark, watermark, watermark, boundary)
        ).fetchall()
        have = {(r["source"], r["source_job_id"]) for r in changed}
        changed = changed + [
            r for r in spillover if (r["source"], r["source_job_id"]) not in have
        ]

    upserted = deleted = skipped = 0
    # A dry run must advance nothing -- including the watermark. Recording
    # one anyway once made a real run right after see "0 seen": the dry
    # run's own finish_run stored this same computed watermark under
    # status='ok', so the next read-back treated everything it only
    # *previewed* as already synced, though nothing had actually been
    # written to Render at all.
    new_watermark = watermark

    # Buffered separately and flushed together (deletes first) whenever
    # either fills up -- see `_BATCH_SIZE`'s docstring for why batching
    # matters here. Two rows sharing one dedup_key inside a single pass
    # (one needing delete, the other upsert) is not something this schema
    # can produce -- `dedup_key` is derived from a local row's own
    # (source, source_job_id), which is already unique -- so there is no
    # ordering hazard in flushing these independently.
    pending_deletes: list[str] = []
    pending_upserts: list[tuple[Any, ...]] = []
    # Index-aligned with the two lists above -- appended at the exact same
    # call sites, drained together -- so the Jobs dashboard can show whether
    # a row actually reached Render, not just render_sync's own run-level
    # watermark. Local writes only, batched once per flush (never per row on
    # the success path), so this adds no round-trips to the hot path.
    pending_delete_local_keys: list[tuple[str, str]] = []
    pending_upsert_local_keys: list[tuple[str, str]] = []

    def stamp_local_sync(keys: list[tuple[str, str]], sync_status: str) -> None:
        if not keys or dry_run:
            return
        placeholders = ", ".join(["(?, ?)"] * len(keys))
        conn.execute(
            f"UPDATE jobs SET render_synced_at = ?, render_sync_status = ? "
            f"WHERE (source, source_job_id) IN ({placeholders})",
            (datetime.now(timezone.utc), sync_status, *[v for pair in keys for v in pair]),
        )
        conn.commit()

    def flush_deletes() -> None:
        nonlocal deleted, skipped
        if not pending_deletes:
            return
        keys, pending_deletes[:] = pending_deletes[:], []
        local_keys, pending_delete_local_keys[:] = pending_delete_local_keys[:], []
        if len(keys) == 1:
            try:
                render_conn.execute(_DELETE_SQL, (keys[0],))
                deleted += 1
                stamp_local_sync(local_keys, "synced")
            except IntegrityError:
                render_conn.rollback()
                skipped += 1
                stamp_local_sync(local_keys, "blocked")
                log.warning(
                    "render-sync: %s has a dependent review — leaving it "
                    "in Render rather than deleting", keys[0],
                )
            return
        try:
            render_conn.execute(_batch_delete_sql(len(keys)), keys)
            deleted += len(keys)
            stamp_local_sync(local_keys, "synced")
        except IntegrityError:
            # Render's own schema, not jobscrape's to manage (see this
            # module's docstring) — a row a site visitor has reviewed is
            # referenced by `reviews.job_id`, so a hard DELETE violates that
            # foreign key, and Postgres aborts the WHOLE statement, not just
            # the offending row. One un-deletable row must not cost this
            # entire batch -- fall back to one row at a time, just for this
            # batch, to isolate exactly which key(s) are blocked.
            render_conn.rollback()
            for key, local_key in zip(keys, local_keys):
                try:
                    render_conn.execute(_DELETE_SQL, (key,))
                    deleted += 1
                    stamp_local_sync([local_key], "synced")
                except IntegrityError:
                    render_conn.rollback()
                    skipped += 1
                    stamp_local_sync([local_key], "blocked")
                    log.warning(
                        "render-sync: %s has a dependent review — leaving "
                        "it in Render rather than deleting", key,
                    )
        render_conn.commit()

    def flush_upserts() -> None:
        nonlocal upserted, skipped
        if not pending_upserts:
            return
        rows, pending_upserts[:] = pending_upserts[:], []
        local_keys, pending_upsert_local_keys[:] = pending_upsert_local_keys[:], []
        if len(rows) == 1:
            try:
                render_conn.execute(_UPSERT_SQL, rows[0])
                upserted += 1
                render_conn.commit()
                stamp_local_sync(local_keys, "synced")
            except IntegrityError as exc:
                render_conn.rollback()
                skipped += 1
                stamp_local_sync(local_keys, "blocked")
                log.warning(
                    "render-sync: %s rejected by Render (%s) — skipping this upsert",
                    rows[0][2], exc,
                )
            return
        try:
            render_conn.execute(
                _batch_upsert_sql(len(rows)),
                [v for row in rows for v in row],
            )
            upserted += len(rows)
            render_conn.commit()
            stamp_local_sync(local_keys, "synced")
        except IntegrityError:
            # One bad row (e.g. a constraint Render's schema enforces that
            # this batch happens to violate) must not cost the whole batch —
            # the same per-row fallback `flush_deletes()` uses for a delete
            # blocked by Render's `reviews.job_id` foreign key.
            render_conn.rollback()
            for row, local_key in zip(rows, local_keys):
                try:
                    render_conn.execute(_UPSERT_SQL, row)
                    upserted += 1
                    render_conn.commit()
                    stamp_local_sync([local_key], "synced")
                except IntegrityError as exc:
                    render_conn.rollback()
                    skipped += 1
                    stamp_local_sync([local_key], "blocked")
                    log.warning(
                        "render-sync: %s rejected by Render (%s) — skipping "
                        "this upsert", row[2], exc,
                    )

    def flush_pending() -> None:
        # Deletes first: a row that closed makes room for whatever comes
        # after it under the same dedup_key sooner, matching this loop's
        # own changed_at-ordered processing as closely as batching allows.
        flush_deletes()
        flush_upserts()

    for job_row in changed:
        changed_at = job_row.get("changed_at")
        if not dry_run and changed_at and changed_at > new_watermark:
            new_watermark = changed_at

        # A closed posting and a row dedupe just suppressed are both cases
        # where Render should no longer show this row's OWN dedup_key --
        # a suppressed row's posting may still be live under its canonical's
        # own key, but this row is not that key, and leaving it in Render
        # keeps a phantom duplicate on the site.
        if job_row.get("closed_at") or job_row.get("duplicate_of"):
            if dry_run:
                deleted += 1
                continue
            dedup_key = _dedup_key(
                job_row.get("source") or "", job_row.get("source_job_id"),
                job_row.get("apply_url") or job_row.get("job_url"),
                job_row.get("title") or "", job_row.get("company") or "",
            )
            pending_deletes.append(dedup_key)
            pending_delete_local_keys.append(
                (job_row.get("source") or "", job_row.get("source_job_id") or "")
            )
            if len(pending_deletes) >= _BATCH_SIZE or len(pending_upserts) >= _BATCH_SIZE:
                flush_pending()
            continue

        if dry_run:
            upserted += 1
            continue
        values = _to_render_row(job_row)
        pending_upserts.append((
            values["external_job_id"], values["source_platform"],
            values["dedup_key"], values["title"], values["company"],
            values["location"], values["salary_min"], values["salary_max"],
            values["jd_url"], values["jd_text"], values["posted_date"],
            values["created_at"], values["skills"], values["domain"],
            values["level"],
        ))
        pending_upsert_local_keys.append(
            (job_row.get("source") or "", job_row.get("source_job_id") or "")
        )
        if len(pending_upserts) >= _BATCH_SIZE or len(pending_deletes) >= _BATCH_SIZE:
            flush_pending()

    if not dry_run:
        flush_pending()

    return {
        "seen": len(changed), "upserted": upserted, "deleted": deleted,
        "skipped": skipped, "watermark": new_watermark.isoformat(), "dry_run": dry_run,
    }


@_serialized
def backfill_local_sync_status(
    conn: db.Connection, render_conn: db.Connection, *, dry_run: bool = False,
) -> dict[str, Any]:
    """One-time catch-up for `render_synced_at`/`render_sync_status`.

    Those two columns didn't exist until this session added them; every row
    `sync()` had already pushed to Render before that point has no local
    record of it. Because `sync()` only ever re-examines rows whose
    `changed_at` is newer than its own watermark, an old row that hasn't
    changed since is never re-visited — it would show 'pending' forever even
    though it's genuinely already on Render, with no future incremental pass
    ever able to correct it.

    This fixes that by asking Render directly rather than guessing: pull
    every `dedup_key` Render actually has, compute the same key locally
    (`_dedup_key`, the exact rule Render's own upsert keys on), and stamp
    'synced' on any local row that matches. Deliberately excludes closed and
    duplicate rows even if they still exist in Render — clearing anything
    for them via `render_sync_status` still needs `sync()`'s own DELETE
    path; marking one 'synced' here would just make it look like nothing
    is left to do.
    """
    render_keys: set[str] = {
        r["dedup_key"] for r in render_conn.execute("SELECT dedup_key FROM jobs")
    }

    candidates = conn.execute(
        "SELECT source, source_job_id, apply_url, job_url, title, company "
        "FROM jobs WHERE render_synced_at IS NULL "
        "  AND closed_at IS NULL AND duplicate_of IS NULL"
    ).fetchall()

    matched_keys: list[tuple[str, str]] = []
    for row in candidates:
        source = row["source"] or ""
        source_job_id = row["source_job_id"] or ""
        dedup_key = _dedup_key(
            source, source_job_id, row["apply_url"] or row["job_url"],
            row["title"] or "", row["company"] or "",
        )
        if dedup_key in render_keys:
            matched_keys.append((source, source_job_id))

    stamped = 0
    if not dry_run:
        now = datetime.now(timezone.utc)
        for i in range(0, len(matched_keys), _BATCH_SIZE):
            batch = matched_keys[i : i + _BATCH_SIZE]
            placeholders = ", ".join(["(?, ?)"] * len(batch))
            conn.execute(
                f"UPDATE jobs SET render_synced_at = ?, render_sync_status = 'synced' "
                f"WHERE (source, source_job_id) IN ({placeholders})",
                (now, *[v for pair in batch for v in pair]),
            )
            conn.commit()
            stamped += len(batch)

    return {
        "candidates": len(candidates), "matched": len(matched_keys),
        "stamped": stamped, "dry_run": dry_run,
    }


@_serialized
def delete_source(
    conn: db.Connection, render_conn: db.Connection, source: str, *, dry_run: bool = False,
) -> dict[str, Any]:
    """Remove every job from ``source`` from both this database and Render.

    For turning a source off for good (see `sources.OFF_BY_DEFAULT`) and
    wanting its existing archive gone too, not just stopped from growing.

    Order matters: Render is deleted FIRST, using each row's own
    `apply_url`/`job_url`/`title`/`company` to compute its `dedup_key` — once
    a row is gone from `jobs`, `sync()`'s changed-since sweep can never see it
    again to issue the matching Render DELETE on its own, so this does that
    side itself, directly, with the same per-row IntegrityError fallback
    `flush_deletes()` uses for a delete blocked by Render's `reviews.job_id`
    foreign key. `db.delete_jobs` runs after, for the local side — it already
    releases any surviving duplicate that pointed at a doomed row here as its
    canonical, and bumps `duplicate_marked_at` so the next `sync()` pass picks
    up the newly-released row as a fresh upsert.
    """
    rows = conn.execute(
        "SELECT source, source_job_id, apply_url, job_url, title, company "
        "FROM jobs WHERE source = ?", (source,)
    ).fetchall()
    keys = [(r["source"], r["source_job_id"]) for r in rows]
    dedup_keys = [
        _dedup_key(
            r["source"], r["source_job_id"], r["apply_url"] or r["job_url"],
            r["title"] or "", r["company"] or "",
        )
        for r in rows
    ]

    render_deleted = render_blocked = 0
    if not dry_run:
        for i in range(0, len(dedup_keys), _BATCH_SIZE):
            batch = dedup_keys[i:i + _BATCH_SIZE]
            if len(batch) == 1:
                try:
                    render_conn.execute(_DELETE_SQL, (batch[0],))
                    render_conn.commit()
                    render_deleted += 1
                except IntegrityError:
                    render_conn.rollback()
                    render_blocked += 1
                    log.warning(
                        "delete-source %s: %s has a dependent review — "
                        "leaving it in Render rather than deleting",
                        source, batch[0],
                    )
                continue
            try:
                render_conn.execute(_batch_delete_sql(len(batch)), batch)
                render_conn.commit()
                render_deleted += len(batch)
            except IntegrityError:
                render_conn.rollback()
                for key in batch:
                    try:
                        render_conn.execute(_DELETE_SQL, (key,))
                        render_conn.commit()
                        render_deleted += 1
                    except IntegrityError:
                        render_conn.rollback()
                        render_blocked += 1
                        log.warning(
                            "delete-source %s: %s has a dependent review — "
                            "leaving it in Render rather than deleting",
                            source, key,
                        )

    local_deleted = local_released = 0
    if not dry_run:
        local_deleted, local_released = db.delete_jobs(conn, keys)

    return {
        "source": source, "found": len(keys),
        "render_deleted": render_deleted, "render_blocked": render_blocked,
        "local_deleted": local_deleted, "local_released": local_released,
        "dry_run": dry_run,
    }
