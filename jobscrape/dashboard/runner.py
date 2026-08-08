"""Start, monitor and stop pipeline stages from the dashboard.

Stages run as **subprocesses of the existing CLI** rather than in-process. That
buys three things worth more than the small overhead:

* No duplicated logic — the dashboard runs exactly what the terminal runs.
* A long sweep can genuinely be killed, including its child work.
* A crash in a stage cannot take the web server down with it.

Output is streamed into ``run_events`` so the browser can tail it and so the
history survives a dashboard restart.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .. import config, db

log = logging.getLogger("jobscrape.dashboard.runner")

#: Stage name -> CLI arguments. Everything the dashboard can launch is here, so
#: the web layer can never assemble an arbitrary command line.
#:
#: The stage name was always allowlisted; the caller-supplied `extra` was not,
#: and was appended to argv verbatim — see `SAFE_EXTRA_FLAGS` below for why that
#: made the sentence above untrue.
STAGES: dict[str, list[str]] = {
    "discover": ["discover"],
    "discover-fast": ["discover", "--vector", "ct", "--vector", "github"],
    "validate": ["validate", "--concurrency", "6"],
    # `fetch`, `apify-pull` and `jobright-fetch` each chain their own
    # dedupe+stats internally now (see `cli.py`'s `cmd_fetch`/
    # `cmd_apify_pull`/`cmd_jobright_fetch`) — every fetch-type command gets
    # the same safety net, not just a composite. That's also why each is
    # listed in `STAGE_GROUPS` below reserving `dedupe`/`stats` too.
    "fetch": ["fetch"],
    # Split for near-real-time coverage: `fetch` now sweeps every ATS/global
    # source concurrently (one connection each, see `cli.cmd_fetch`), so wall
    # -clock time for the whole registry is bounded by the SLOWEST single
    # source — measured on the live archive, that's Workday alone at ~2h
    # while every other source finishes in 2-31 minutes (Workday has no
    # "list everything" endpoint, unlike the rest, and pages up to
    # `config.WORKDAY_MAX_PER_BOARD` per tenant). Scheduling "fetch" itself
    # tighter than ~2h would just make every tick after the first skip
    # (RunManager's one-active-run-per-stage) without buying anything, since
    # Workday would still be mid-sweep. These two run independently instead
    # — everything except Workday on a tight interval, Workday on its own
    # much longer one — so the fast sources actually get to be fast.
    "fetch-fast": ["fetch", "--exclude-sources", "workday"],
    "fetch-workday": ["fetch", "--sources", "workday"],
    "export": ["export", "--format", "csv", "--format", "xlsx"],
    "apify-pull": ["apify-pull", "--last", "3"],
    "dedupe": ["dedupe", "--backfill"],
    "purge": ["purge"],
    "liveness": ["liveness"],
    "stats": ["stats"],
    "sources": ["sources"],
    # Roles and work-model are fixed here rather than caller-supplied, unlike
    # the `window` below — a static preset, the same shape as "apify-pull"'s
    # hardcoded `--last 3`, not a new value-taking input the web layer could
    # ever be tricked into repointing (see `SAFE_EXTRA_FLAGS`'s docstring for
    # why that distinction matters). `--chrome-version` is appended in
    # `start()` from config instead of baked in here, since it describes this
    # machine's installed Chrome, not a property of the stage itself.
    "jobright-fetch": [
        "jobright-fetch", "--work-model", "remote", "--roles",
        "Backend Engineer,Full Stack Engineer,Python Engineer,Java Engineer,"
        "C/C++ Engineer,.Net Engineer,Golang Engineer,Data Engineer,"
        "Machine Learning Engineer,AI Engineer,LLM Engineer,"
        "Frontend Software Engineer,React Developer,Android Developer,"
        "iOS/Swift Developer,Flutter Developer,DevOps,Site Reliability Engineer",
    ],
    # Pushes to a separate Render-hosted database — see
    # `jobscrape/render_sync.py`. Fired automatically by `scheduler._fire()`
    # after any Schedule-tab run finishes, not meant to be clicked directly,
    # but still a plain STAGES entry so it shows up in Runs history like
    # everything else and `--dry-run` rides the existing extra allowlist.
    "render-sync": ["render-sync"],
    # Composite: the exact sequence `scripts/schedule.sh` runs, ported to
    # `jobscrape schedule-run` so the dashboard calls one subprocess rather
    # than launching five separately with no way to treat them as one unit.
    "daily-schedule": ["schedule-run", "--mode", "daily"],
    "weekly-schedule": ["schedule-run", "--mode", "weekly"],
    # The one-button "give me everything" run: every board, every currently
    # open posting, no date window and no validation cap — the Runs page's
    # single primary action, as opposed to clicking each stage separately.
    "full-sweep": ["schedule-run", "--mode", "full"],
    # Discover + validate only, no fetch/apify-pull — for a schedule that
    # just wants the board registry itself kept fresh (new boards found,
    # existing ones revalidated) on its own cadence, independent of how
    # often postings are actually fetched.
    "discover-validate": ["schedule-run", "--mode", "sources"],
}

#: `purge` deletes data for real unless `extra` carries `--dry-run` (already on
#: `SAFE_EXTRA_FLAGS`). This module only allowlists what CAN be run — the
#: dashboard's "Run now" control for `purge` is what must default `extra` to
#: `--dry-run` and require an explicit confirm step before ever omitting it.

#: Stages that are really steps of a composite ("daily-schedule",
#: "weekly-schedule"), all running inside that ONE subprocess. Without this, a
#: manual click on plain "discover" while "daily-schedule" is mid-run would
#: race the same `companies`/`board_health` rows the composite's own discover
#: step is touching — precisely what `flock` in `scripts/schedule.sh` exists
#: to prevent, just between a different pair of callers.
STAGE_GROUPS: dict[str, tuple[str, ...]] = {
    "daily-schedule": (
        "discover", "validate", "fetch", "fetch-fast", "fetch-workday",
        "apify-pull", "dedupe", "stats",
    ),
    "weekly-schedule": (
        "discover", "validate", "fetch", "fetch-fast", "fetch-workday",
        "dedupe", "stats",
    ),
    "full-sweep": (
        "discover", "validate", "fetch", "fetch-fast", "fetch-workday",
        "apify-pull", "dedupe", "stats",
    ),
    # `fetch`/`apify-pull`/`jobright-fetch` each chain their own dedupe+stats
    # internally now (see `cli.py`), but deliberately have NO entry here —
    # unlike the composites above, nothing stops them running side by side.
    # They pull from unrelated sources, and the one thing that genuinely
    # can't run twice at once (`dedupe --backfill`'s full-archive rewrite)
    # is protected by a Postgres advisory lock in `cmd_dedupe` instead —
    # whichever of them reaches its own dedupe step second just waits for
    # the lock, invisible to RunManager, rather than being rejected here.
    #
    # `fetch-fast`/`fetch-workday` are the one exception: both are scoped
    # SLICES of plain `fetch` (all sources except Workday / Workday only),
    # so unlike `apify-pull`/`jobright-fetch` (genuinely unrelated sources)
    # they overlap heavily with `fetch` itself and must not race it — the
    # same "two concurrent fetches race the same `board_health`/
    # `zero_yield` rows" hazard `scripts/schedule.sh` guards daily-vs-weekly
    # against, just between plain `fetch` and a slice of it. `fetch` lists
    # BOTH as reserved keys, which is enough on its own for BOTH
    # directions: while `fetch` runs, its own reservation marks
    # `fetch-fast`/`fetch-workday` active too, so THEIR start-checks (which
    # always include their own name) see the conflict — and while either
    # of them runs, `fetch`'s start-check (which always checks for both by
    # name, regardless of which is active) sees it too. Neither
    # `fetch-fast` nor `fetch-workday` needs — or has — an entry of its
    # own; giving one back to `fetch` would make them ALSO conflict with
    # EACH OTHER (both would reserve the shared "fetch" key), which is
    # wrong: their source sets are disjoint (Workday excluded from one,
    # Workday only in the other), so nothing stops them running side by
    # side, and the composites above must list them directly (not rely on
    # recursing through `fetch`'s entry — see `_reserved_keys`'s docstring
    # and `test_stage_groups_are_pre_flattened`).
    "fetch": ("fetch-fast", "fetch-workday"),
    # `discover-validate` is a plain composite of two already-listed members
    # (both `daily-schedule`/`weekly-schedule`/`full-sweep` already reserve
    # `discover`/`validate` directly) — this entry alone is enough for
    # bidirectional conflict protection: `RunManager._reserved_keys()`
    # checks by key-membership, not by name, so starting `discover-validate`
    # while e.g. `daily-schedule` is active finds `discover` already claimed
    # and refuses, and vice versa. Neither `discover` nor `validate` has its
    # own entry, so `test_stage_groups_are_pre_flattened`'s one-level-only
    # invariant is satisfied trivially.
    "discover-validate": ("discover", "validate"),
}


#: Schedule-tab-only presets: a name -> (base STAGES key, days back). Not
#: `STAGES` entries themselves — these aren't independently launchable
#: stages, just a lookup `scheduler._fire()` resolves into a real stage plus
#: a window computed fresh every time it fires (`date.today() - days`),
#: never a fixed date baked in at schedule-creation time. The Runs page's
#: own date-range cards target the base stages directly instead, with a
#: date the caller picked explicitly.
RELATIVE_WINDOWS: dict[str, tuple[str, int]] = {
    f"{base}-{suffix}": (base, days)
    for base in ("fetch", "apify-pull", "jobright-fetch")
    for suffix, days in (("1d", 1), ("1w", 7), ("2w", 14), ("1m", 30), ("2m", 60))
}


def resolve_stage(
    stage: str,
) -> tuple[str, tuple[date | None, date | None] | None]:
    """Translate a caller-supplied stage name into what `start()` needs.

    A real `STAGES` key passes straight through with no window. A
    `RELATIVE_WINDOWS` preset name (e.g. "fetch-1w") resolves to its real
    base stage plus a `since` computed fresh on *this* call — never a fixed
    date, so the same schedule row means something different (and correct)
    every time it's fired or run by hand.

    The one function both `scheduler._fire()` and the dashboard's "Run now"
    route call, so a schedule behaves identically whichever way it's
    triggered — before this existed, "Run now" passed the preset name
    straight to `start()`, which doesn't know it, and rejected it as an
    unknown stage.
    """
    relative = RELATIVE_WINDOWS.get(stage)
    if relative is None:
        return stage, None
    base_stage, days = relative
    return base_stage, (date.today() - timedelta(days=days), None)


def _reserved_keys(stage: str) -> tuple[str, ...]:
    """Every `_active` key a run of `stage` occupies: itself, plus every plain
    stage in its group if it is a composite."""
    return (stage, *STAGE_GROUPS.get(stage, ()))

#: The only extra CLI flags a dashboard caller may add to a stage.
#:
#: Every one is a value-less boolean, which is the property that matters: a flag
#: that takes a value can redirect where the process writes. `--output` (on the
#: `export` stage) and `--db` both do, so `extra="--output ~/.zshrc"` was an
#: arbitrary file write, and `--db` repointed the archive.
#:
#: That was reachable without any credential. These routes take `Form(...)`
#: bodies, so a cross-origin form POST is a CORS "simple request" — no
#: preflight, the browser just sends it — and binding to 127.0.0.1 is no defence
#: against a page the operator already has open. There is no CSRF token here
#: because there is no session to protect; the allowlist is what makes the
#: absence of one safe.
SAFE_EXTRA_FLAGS = frozenset({
    "--recheck", "--all-roles", "--worldwide", "--no-cache",
    "--dry-run", "--include-restricted", "--complete-only",
})


def safe_extra(extra: list[str] | None) -> list[str]:
    """Keep only allowlisted flags, discarding anything else.

    Silently dropping rather than raising: this is defence against a request the
    operator did not knowingly make, so failing the run would turn a blocked
    attack into a denial of service on a legitimate button press.
    """
    if not extra:
        return []
    kept, refused = [], []
    for token in extra:
        (kept if token in SAFE_EXTRA_FLAGS else refused).append(token)
    if refused:
        log.warning("ignoring CLI flags not on the dashboard allowlist: %s", refused)
    return kept


#: Progress lines the CLI already emits, parsed for the live counters.
_DONE_RE = re.compile(
    r"(?P<source>\S+)\s+done: (?P<boards>\d+) boards, (?P<raw>\d+) postings seen, "
    r"(?P<matched>\d+) matched, (?P<new>\d+) new"
)
_PROBED_RE = re.compile(r"(?P<ats>\S+): (?P<done>\d+)/(?P<total>\d+) probed")
_FETCHING_RE = re.compile(r"(?P<source>\S+)\s+fetching (?P<total>\d+) boards")


@dataclass
class ActiveRun:
    run_id: int
    stage: str
    command: str
    process: Any = None
    started_at: str = ""
    progress: dict[str, Any] = field(default_factory=dict)

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process else None


class RunManager:
    """Owns at most one active run per stage."""

    def __init__(self, database_url: str | None = None) -> None:
        #: kept as `db_path` for compatibility with existing call sites — it
        #: is passed straight through to `db.session()`, which now treats it
        #: as an optional DATABASE_URL override rather than a file path.
        self.db_path = database_url
        self._active: dict[str, ActiveRun] = {}
        self._lock = asyncio.Lock()
        #: Strong references to the log-consumer tasks.
        #:
        #: asyncio keeps only a WEAK reference to a running task, so a task
        #: nobody holds can be garbage-collected mid-flight. `_consume` is what
        #: streams a run's output into `run_events` and marks the run finished,
        #: so losing it would stop the live log dead and leave the run stuck at
        #: "running" forever — with no error anywhere to explain it.
        self._consumers: set[asyncio.Task[None]] = set()

    # -- state -------------------------------------------------------------
    def active(self) -> list[dict[str, Any]]:
        # A composite occupies several keys in `_active` (itself plus every
        # plain stage in its group — see `_reserved_keys`), all pointing at
        # the same `ActiveRun`. Dedupe by run_id so it appears once.
        seen: set[int] = set()
        out = []
        for r in self._active.values():
            if r.run_id in seen:
                continue
            seen.add(r.run_id)
            out.append({
                "run_id": r.run_id, "stage": r.stage, "command": r.command,
                "pid": r.pid, "started_at": r.started_at, "progress": r.progress,
            })
        return out

    def is_running(self, stage: str) -> bool:
        return stage in self._active

    def any_running(self) -> bool:
        return bool(self._active)

    # -- lifecycle ---------------------------------------------------------
    async def start(
        self,
        stage: str,
        extra: list[str] | None = None,
        window: tuple[date | None, date | None] | None = None,
    ) -> dict[str, Any]:
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        async with self._lock:
            keys = _reserved_keys(stage)
            busy = next((k for k in keys if k in self._active), None)
            if busy is not None:
                blocker = self._active[busy].stage
                raise RuntimeError(
                    f"{stage} is already running" if blocker == stage
                    else f"{stage} conflicts with the running {blocker}"
                )

            # `window` is built ONLY from `date` objects the caller (the
            # dashboard route) already validated with `filters.parse_date` —
            # never from a raw string. That's what keeps this different from
            # `extra`: a value-taking flag like `--since` must never ride the
            # free-text allowlist (see `SAFE_EXTRA_FLAGS`'s docstring), but a
            # `date` object has no way to smuggle an arbitrary token, so
            # building `--since <isoformat>` from one here is safe.
            window_argv: list[str] = []
            if window:
                since, until = window
                if since:
                    window_argv += ["--since", since.isoformat()]
                if until:
                    window_argv += ["--until", until.isoformat()]

            # Which Chrome is installed is a property of this machine, not of
            # the stage, so it's appended here from config rather than baked
            # into STAGES — the same reasoning as `window_argv` above, just
            # sourced from config instead of a call argument.
            chrome_argv: list[str] = []
            if stage == "jobright-fetch" and config.JOBRIGHT_CHROME_VERSION:
                chrome_argv = ["--chrome-version", str(config.JOBRIGHT_CHROME_VERSION)]

            argv = [sys.executable, "-m", "jobscrape", *STAGES[stage],
                    *safe_extra(extra), *window_argv, *chrome_argv]
            command = " ".join(argv[2:])

            with db.session(self.db_path) as conn:
                run_id = db.start_run(conn, command, stage)

            # Hand the run id down so the CLI records into this row rather
            # than opening a duplicate.
            env = {**os.environ, "JOBSCRAPE_RUN_ID": str(run_id)}
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(config.PROJECT_ROOT),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # Its own process group, so stopping kills the whole tree
                # rather than orphaning the workers.
                start_new_session=True,
            )
            run = ActiveRun(
                run_id=run_id, stage=stage, command=command, process=process,
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            # Reserve every key this run occupies (itself, plus its group's
            # plain stages if it is a composite) so a concurrent call to any
            # of them is refused above rather than racing this run.
            for key in keys:
                self._active[key] = run

            with db.session(self.db_path) as conn:
                conn.execute("UPDATE runs SET pid = ? WHERE id = ?", (process.pid, run_id))
                conn.commit()

        consumer = asyncio.create_task(self._consume(run))
        self._consumers.add(consumer)
        consumer.add_done_callback(self._consumers.discard)
        return {"run_id": run_id, "stage": stage, "pid": process.pid}

    async def stop(self, stage: str) -> bool:
        run = self._active.get(stage)
        if not run or not run.process:
            return False
        try:
            # Signal the whole group: the CLI spawns concurrent workers.
            os.killpg(os.getpgid(run.process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                run.process.terminate()
            except ProcessLookupError:
                return False
        return True

    # -- output ------------------------------------------------------------
    async def _consume(self, run: ActiveRun) -> None:
        """Stream the subprocess's output into run_events until it exits."""
        assert run.process and run.process.stdout
        buffer: list[tuple[str, dict[str, Any] | None]] = []

        try:
            async for raw in run.process.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                counters = self._parse(run, line)
                buffer.append((line, counters))
                # Flush in batches: a busy sweep emits far more lines than the
                # browser needs written one-by-one.
                if len(buffer) >= 10:
                    self._flush(run, buffer)
                    buffer = []
        except Exception as exc:
            log.debug("stream ended for run %s: %s", run.run_id, exc)

        if buffer:
            self._flush(run, buffer)

        code = await run.process.wait()
        status = "ok" if code == 0 else ("stopped" if code in (-15, -2, 143) else "failed")
        with db.session(self.db_path) as conn:
            existing = conn.execute(
                "SELECT stats FROM runs WHERE id = ?", (run.run_id,)
            ).fetchone()
            # The actual failure reason is already sitting in run_events —
            # `_flush()` below classifies any line containing " ERROR " as
            # level='error' as the run streams — just never read back and
            # attached to the run itself until now.
            error_text = None
            if status == "failed":
                error_rows = db.recent_error_events(conn, run.run_id)
                if error_rows:
                    error_text = "\n".join(r["message"] for r in error_rows)
            # cmd_fetch writes its own rich stats; don't clobber them.
            if not (existing and existing["stats"]):
                db.finish_run(conn, run.run_id, status, {"progress": run.progress},
                               error=error_text)
            else:
                conn.execute(
                    "UPDATE runs SET ended_at = ?, status = ?, error = ? WHERE id = ?",
                    (db.now_iso(), status, error_text, run.run_id),
                )
                conn.commit()

        for key in _reserved_keys(run.stage):
            self._active.pop(key, None)
        log.info("run %s (%s) finished: %s", run.run_id, run.stage, status)

    def _flush(self, run: ActiveRun, buffer: list[tuple[str, dict[str, Any] | None]]) -> None:
        with db.session(self.db_path) as conn:
            for message, counters in buffer:
                level = (
                    "error" if " ERROR " in message
                    else "warn" if " WARNING " in message
                    else "info"
                )
                db.log_event(conn, run.run_id, message, stage=run.stage,
                             level=level, counters=counters)
            conn.commit()

    def _parse(self, run: ActiveRun, line: str) -> dict[str, Any] | None:
        """Pull structured progress out of the CLI's existing log lines."""
        match = _FETCHING_RE.search(line)
        if match:
            run.progress["current"] = match.group("source")
            run.progress["boards_total"] = int(match.group("total"))
            run.progress["boards_done"] = 0
            return {"boards_total": int(match.group("total"))}

        match = _DONE_RE.search(line)
        if match:
            source = match.group("source")
            counters = {
                "boards": int(match.group("boards")),
                "seen": int(match.group("raw")),
                "matched": int(match.group("matched")),
                "new": int(match.group("new")),
            }
            run.progress.setdefault("sources", {})[source] = counters
            run.progress["jobs_new"] = sum(
                s.get("new", 0) for s in run.progress["sources"].values()
            )
            return counters

        match = _PROBED_RE.search(line)
        if match:
            run.progress["current"] = match.group("ats")
            run.progress["boards_done"] = int(match.group("done"))
            run.progress["boards_total"] = int(match.group("total"))
            return {"probed": int(match.group("done")), "total": int(match.group("total"))}
        return None


#: One manager per process; the app imports this.
manager = RunManager()
