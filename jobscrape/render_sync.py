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

import logging
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

    if limit is not None and len(changed) == limit:
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
        nonlocal upserted
        if not pending_upserts:
            return
        rows, pending_upserts[:] = pending_upserts[:], []
        local_keys, pending_upsert_local_keys[:] = pending_upsert_local_keys[:], []
        if len(rows) == 1:
            render_conn.execute(_UPSERT_SQL, rows[0])
        else:
            render_conn.execute(
                _batch_upsert_sql(len(rows)),
                [v for row in rows for v in row],
            )
        upserted += len(rows)
        render_conn.commit()
        stamp_local_sync(local_keys, "synced")

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
