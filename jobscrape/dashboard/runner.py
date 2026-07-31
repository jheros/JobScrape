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
from datetime import datetime, timezone
from typing import Any

from .. import config, db

log = logging.getLogger("jobscrape.dashboard.runner")

#: Stage name -> CLI arguments. Everything the dashboard can launch is here, so
#: the web layer can never assemble an arbitrary command line.
STAGES: dict[str, list[str]] = {
    "discover": ["discover"],
    "discover-fast": ["discover", "--vector", "ct", "--vector", "github"],
    "validate": ["validate", "--concurrency", "6"],
    "fetch": ["fetch"],
    "export": ["export", "--format", "csv", "--format", "xlsx"],
}

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
        return [
            {
                "run_id": r.run_id, "stage": r.stage, "command": r.command,
                "pid": r.pid, "started_at": r.started_at, "progress": r.progress,
            }
            for r in self._active.values()
        ]

    def is_running(self, stage: str) -> bool:
        return stage in self._active

    def any_running(self) -> bool:
        return bool(self._active)

    # -- lifecycle ---------------------------------------------------------
    async def start(self, stage: str, extra: list[str] | None = None) -> dict[str, Any]:
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        async with self._lock:
            if stage in self._active:
                raise RuntimeError(f"{stage} is already running")

            argv = [sys.executable, "-m", "jobscrape", *STAGES[stage], *(extra or [])]
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
            self._active[stage] = run

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
            # cmd_fetch writes its own rich stats; don't clobber them.
            if not (existing and existing["stats"]):
                db.finish_run(conn, run.run_id, status, {"progress": run.progress})
            else:
                conn.execute(
                    "UPDATE runs SET ended_at = ?, status = ? WHERE id = ?",
                    (db.now_iso(), status, run.run_id),
                )
                conn.commit()

        self._active.pop(run.stage, None)
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
