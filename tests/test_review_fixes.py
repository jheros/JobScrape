"""Regressions found by a full-project review.

Each test here corresponds to a defect that shipped and was not caught by the
existing suite. The comments record *why* each was invisible, since that is the
part worth not repeating.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jobscrape import db
from jobscrape.models import Job
from jobscrape.sources.base import shallow_refresh


# --- raw_json was being wiped on every shallow refresh ---------------------
#
# `raw` is in `PRESERVED_ON_REFRESH` and `raw_json` is in `upsert_jobs`'s
# COALESCE preserve set, so the intent was always to keep the payload a deep
# pass captured. But `to_row()` serialised an empty dict to the *string* "{}",
# which is not NULL, so COALESCE happily took it and overwrote good data.
#
# Invisible because every existing test asserted on the columns a shallow
# refresh is *known* to preserve (description, salary, dates). Nothing looked at
# raw_json, and nothing downstream reads it — so the loss was silent until a
# backfill needed the payload and found "{}". 771 rows in the archive, 13% of
# Ashby's, and it is why Oracle's native WorkplaceType could not be backfilled.


def test_empty_raw_serialises_to_null_not_a_string() -> None:
    empty = Job(source="t", source_job_id="1", title="X", company="A", job_url="u")
    assert empty.to_row()["raw_json"] is None, (
        'an empty payload must be NULL so the upsert\'s COALESCE preserves the '
        'stored one; "{}" is truthy and overwrites it'
    )


def test_a_real_payload_still_serialises() -> None:
    job = Job(source="t", source_job_id="1", title="X", company="A", job_url="u",
              raw={"WorkplaceType": "Hybrid"})
    assert job.to_row()["raw_json"] == '{"WorkplaceType": "Hybrid"}'


def test_shallow_refresh_keeps_the_stored_payload(_isolated_db) -> None:
    """The end-to-end case: a repeat sweep must not erase what it captured.

    This is what a second pass over any board actually does — adapters emit
    `raw={} if not with_details`, and `shallow_refresh` blanks it besides.
    """
    conn = _isolated_db
    db.upsert_jobs(conn, [Job(
        source="oracle", source_job_id="19247", title="Principal Platform Engineer",
        company="acme", job_url="https://x/1",
        raw={"WorkplaceType": "Hybrid", "PrimaryLocation": "Norcross, GA"},
    )])
    stored = conn.execute(
        "SELECT raw_json FROM jobs WHERE source_job_id = ?", ("19247",)
    ).fetchone()["raw_json"]
    assert stored and stored.get("WorkplaceType") == "Hybrid"

    db.upsert_jobs(conn, [shallow_refresh(Job(
        source="oracle", source_job_id="19247", title="Principal Platform Engineer",
        company="acme", job_url="https://x/1",
    ))])
    after = conn.execute(
        "SELECT raw_json FROM jobs WHERE source_job_id = ?", ("19247",)
    ).fetchone()["raw_json"]
    assert after and after.get("WorkplaceType") == "Hybrid", (
        "a shallow refresh erased the stored payload"
    )


def test_a_genuinely_new_payload_still_replaces_the_old_one() -> None:
    """The guard must not freeze the column — only blank must be preserved."""
    job = Job(source="t", source_job_id="1", title="X", company="A", job_url="u",
              raw={"new": True})
    assert job.to_row()["raw_json"] == '{"new": true}'


# --- the log-consumer task could be garbage-collected ----------------------


def test_run_manager_holds_a_reference_to_its_consumer_tasks() -> None:
    """asyncio keeps only a WEAK reference to a running task.

    `_consume` streams a run's output into `run_events` and marks the run
    finished. Unreferenced, the garbage collector was free to destroy it
    mid-flight — the live log would stop dead and the run would sit at
    "running" forever, with no error anywhere to explain it.

    `runner.py` had no tests at all, which is why this survived.
    """
    from jobscrape.dashboard.runner import RunManager

    manager = RunManager()
    assert hasattr(manager, "_consumers"), "no strong reference held"

    async def scenario() -> None:
        started = asyncio.Event()
        finish = asyncio.Event()

        async def fake_consume() -> None:
            started.set()
            await finish.wait()

        task = asyncio.create_task(fake_consume())
        manager._consumers.add(task)
        task.add_done_callback(manager._consumers.discard)

        await started.wait()
        assert task in manager._consumers, "task must be held while running"
        finish.set()
        await task
        # ...and released once done, so the set cannot grow without bound
        # across a long-lived dashboard session.
        await asyncio.sleep(0)
        assert task not in manager._consumers, "finished task must be discarded"

    asyncio.run(scenario())


# --- detail results must stay aligned with the jobs they belong to ---------


def test_gather_limited_preserves_length_and_order() -> None:
    """Nine adapters `zip()` a request list against `gather_limited`'s results.

    If that ever returned a different length or order, each posting would get
    another posting's description — plausible-looking, entirely wrong data.
    The `zip(..., strict=True)` at those call sites turns such a change into a
    loud error; this asserts the contract they rely on.
    """
    from jobscrape.http import gather_limited

    async def ok(value: int) -> int:
        await asyncio.sleep(0.001 * (5 - value))   # finish out of order
        return value

    async def boom() -> int:
        raise RuntimeError("this one fails")

    results = asyncio.run(gather_limited([ok(i) for i in range(5)], limit=2))
    assert results == [0, 1, 2, 3, 4], "order must follow the input, not completion"

    mixed = asyncio.run(gather_limited([ok(0), boom(), ok(2)], limit=2))
    assert mixed == [0, None, 2], "a failure must hold its position, not shift the rest"


@pytest.mark.parametrize(
    "module",
    ["adp", "bamboohr", "breezy", "jazzhr", "oracle", "paycom", "pinpoint",
     "rippling", "workday"],
)
def test_detail_zips_are_strict(module: str) -> None:
    """A silent mis-pairing is the worst outcome here, so it must raise."""
    import inspect
    import importlib

    source = inspect.getsource(importlib.import_module(f"jobscrape.sources.{module}"))
    for line in source.splitlines():
        if "zip(" in line and "strict" not in line:
            raise AssertionError(f"{module}: unguarded zip -> {line.strip()}")


# --- the ?-placeholder shim -------------------------------------------------


def test_the_qmark_shim_translates_positionally() -> None:
    from jobscrape.db import _translate_sql

    assert _translate_sql("SELECT * FROM jobs WHERE a = ? AND b = ?") == (
        "SELECT * FROM jobs WHERE a = :p0 AND b = :p1"
    )


def test_the_qmark_trap_is_documented() -> None:
    """Every `?` becomes a parameter, including inside a literal.

    That is a real constraint on anyone writing a query here, and the error it
    produces ("A value is required for bind parameter 'p0'") names nothing that
    would lead you to the cause. The docs next to the shim are the only warning,
    so this asserts they exist.
    """
    import pathlib

    text = pathlib.Path("jobscrape/db.py").read_text()
    header = text[: text.index("_QMARK_RE = re.compile")]
    assert "jsonb_exists" in header, "the JSONB workaround is not documented"
    assert "literal" in header, "the string-literal trap is not documented"
