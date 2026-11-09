"""`endpoint_sync.sync()` against the real test Postgres database and an
`httpx.MockTransport` receiver that records every request it is sent.

Timestamps are written explicitly with Python datetimes rather than `now()`:
inside the one test transaction Postgres's `now()` never moves, so it cannot
order "changed later" rows.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx
import pytest

from jobscrape import db, endpoint_sync
from jobscrape.models import Job

ROW_KEYS = {
    "source", "source_job_id", "title", "company", "job_url", "apply_url",
    "description_text", "workplace_type", "ai_workplace_type", "city", "state",
    "country", "location_raw", "seniority", "ai_seniority", "salary_min",
    "salary_max", "salary_currency", "employment_type", "skills", "department",
    "industry", "ai_requires_clearance", "posted_at", "closed_at", "duplicate_of",
    "scam_signals",
}

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


class Receiver:
    """Records requests; `respond(request, n)` decides each response, where
    `n` counts requests already received by this receiver."""

    def __init__(self, respond: Callable[[httpx.Request, int], httpx.Response] | None = None):
        self.requests: list[httpx.Request] = []
        self._respond = respond or (lambda req, n: ok_for(req))

    def handler(self, request: httpx.Request) -> httpx.Response:
        n = len(self.requests)
        self.requests.append(request)
        return self._respond(request, n)

    def bodies(self, url_part: str = "") -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if url_part in str(r.url)]

    def sent_ids(self, url_part: str = "") -> list[str]:
        return [row["source_job_id"] for b in self.bodies(url_part) for row in b["rows"]]


def ok_for(request: httpx.Request, **counts: Any) -> httpx.Response:
    rows = json.loads(request.content)["rows"]
    payload = {"inserted": len(rows), "updated": 0, "reopened": 0, "closed": 0,
               "skipped": 0, "rejected": []}
    payload.update(counts)
    return httpx.Response(200, json=payload)


def _job(source_job_id: str, **kw: Any) -> Job:
    base = dict(
        source="greenhouse", source_job_id=source_job_id, title="Backend Engineer",
        company="Acme", company_slug="acme", job_url=f"https://x/{source_job_id}",
        posted_at="2026-07-01", description_text="Build things.",
    )
    base.update(kw)
    return Job(**base)  # type: ignore[arg-type]


def _add(conn, *jobs: Job, seen_at: datetime = T0) -> None:
    db.upsert_jobs(conn, list(jobs))
    for job in jobs:
        conn.execute(
            "UPDATE jobs SET last_seen_at = ? WHERE source = ? AND source_job_id = ?",
            (seen_at, job.source, job.source_job_id),
        )


def _endpoint(conn, name: str = "site", url: str = "https://receiver.test/import",
              token: str = "tok-123", enabled: bool = True) -> int:
    endpoint_id = db.create_endpoint(conn, name, url, token, enabled=enabled)
    assert endpoint_id
    return endpoint_id


def _run(conn, receiver: Receiver, **kw: Any) -> dict[str, Any]:
    sleeps: list[float] = kw.pop("sleeps", [])
    client = httpx.Client(transport=httpx.MockTransport(receiver.handler))
    return endpoint_sync.sync(conn, client=client, sleep=sleeps.append, **kw)


# --- what is sent -----------------------------------------------------------


def test_open_row_is_sent_with_exact_keys_and_bearer_token(_isolated_db) -> None:
    conn = _isolated_db
    _endpoint(conn)
    _add(conn, _job("1"))
    receiver = Receiver()

    result = _run(conn, receiver)

    assert len(receiver.requests) == 2  # the rows, then the announce
    request = receiver.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://receiver.test/import"
    assert request.headers["authorization"] == "Bearer tok-123"
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert list(body) == ["rows"]
    [row] = body["rows"]
    assert set(row) == ROW_KEYS
    assert row["posted_at"] == datetime(2026, 7, 1, tzinfo=timezone.utc).isoformat()
    assert row["closed_at"] is None and row["duplicate_of"] is None
    assert result["failed"] == 0


def test_first_sync_skips_closed_and_duplicates_later_syncs_send_them(_isolated_db) -> None:
    conn = _isolated_db
    endpoint_id = _endpoint(conn)
    _add(conn, _job("open"), _job("closed"), _job("dup"))
    canonical = conn.execute("SELECT id FROM jobs WHERE source_job_id = 'open'").fetchone()["id"]
    conn.execute("UPDATE jobs SET closed_at = ? WHERE source_job_id = 'closed'",
                 (T0 + timedelta(minutes=1),))
    conn.execute("UPDATE jobs SET duplicate_of = ?, duplicate_marked_at = ? "
                 "WHERE source_job_id = 'dup'", (canonical, T0 + timedelta(minutes=1)))

    first = Receiver()
    _run(conn, first)
    assert first.sent_ids() == ["open"]
    assert db.get_endpoint(conn, endpoint_id)["cursor_source_job_id"] == "open"

    # Later: the open one closes too.
    conn.execute("UPDATE jobs SET closed_at = ? WHERE source_job_id = 'open'",
                 (T0 + timedelta(minutes=5),))
    second = Receiver()
    _run(conn, second)
    rows = {r["source_job_id"]: r for b in second.bodies() for r in b["rows"]}
    assert set(rows) == {"closed", "dup", "open"}
    assert rows["open"]["closed_at"] == (T0 + timedelta(minutes=5)).isoformat()
    assert rows["closed"]["closed_at"] is not None
    assert rows["dup"]["duplicate_of"] == canonical


def test_restricted_sources_are_never_sent(_isolated_db) -> None:
    conn = _isolated_db
    _endpoint(conn)
    _add(conn, _job("ok"), _job("li", source="linkedin"), _job("in", source="indeed"),
         _job("av", source="avature"))
    receiver = Receiver()
    _run(conn, receiver)
    assert receiver.sent_ids() == ["ok"]


def test_second_run_sends_nothing(_isolated_db) -> None:
    conn = _isolated_db
    _endpoint(conn)
    _add(conn, _job("1"), _job("2"))
    _run(conn, Receiver())
    again = Receiver()
    _run(conn, again)
    assert again.requests == []


def test_tied_changed_at_with_limit_sends_each_row_exactly_once(_isolated_db) -> None:
    conn = _isolated_db
    _endpoint(conn)
    _add(conn, *[_job(f"r{i:02d}") for i in range(40)])  # one shared last_seen_at
    sent: list[str] = []
    for _ in range(3):
        receiver = Receiver()
        _run(conn, receiver, limit=15)
        sent += receiver.sent_ids()
    assert sorted(sent) == sorted(f"r{i:02d}" for i in range(40))
    assert len(sent) == 40


# --- HTTP behaviour ---------------------------------------------------------


def test_a_500_is_retried_then_succeeds(_isolated_db) -> None:
    conn = _isolated_db
    endpoint_id = _endpoint(conn)
    _add(conn, _job("1"))
    receiver = Receiver(lambda req, n: httpx.Response(500) if n == 0 else ok_for(req))
    sleeps: list[float] = []
    result = _run(conn, receiver, sleeps=sleeps)
    assert sleeps == [2]
    assert receiver.sent_ids() == ["1", "1"]
    assert result["failed"] == 0
    assert db.get_endpoint(conn, endpoint_id)["last_sync_status"] == "ok"


def test_retries_give_up_after_three(_isolated_db) -> None:
    conn = _isolated_db
    endpoint_id = _endpoint(conn)
    _add(conn, _job("1"))
    receiver = Receiver(lambda req, n: httpx.Response(503))
    sleeps: list[float] = []
    result = _run(conn, receiver, sleeps=sleeps)
    assert sleeps == [2, 8, 30]
    assert len(receiver.requests) == 4
    assert result["failed"] == 1
    assert db.get_endpoint(conn, endpoint_id)["cursor_changed_at"] is None


def test_a_network_error_is_retried(_isolated_db) -> None:
    conn = _isolated_db
    _endpoint(conn)
    _add(conn, _job("1"))

    def respond(req, n):
        if n == 0:
            raise httpx.ConnectError("refused", request=req)
        return ok_for(req)

    sleeps: list[float] = []
    result = _run(conn, Receiver(respond), sleeps=sleeps)
    assert sleeps == [2] and result["failed"] == 0


def test_a_401_is_not_retried_and_leaves_the_cursor(_isolated_db, caplog) -> None:
    conn = _isolated_db
    endpoint_id = _endpoint(conn)
    _add(conn, _job("1"))
    receiver = Receiver(lambda req, n: httpx.Response(401, text="bad token"))
    sleeps: list[float] = []
    with caplog.at_level(logging.DEBUG):
        result = _run(conn, receiver, sleeps=sleeps)
    assert len(receiver.requests) == 1 and sleeps == []
    assert result["failed"] == 1
    row = db.get_endpoint(conn, endpoint_id)
    assert row["cursor_changed_at"] is None
    assert row["last_sync_status"] == "error"
    assert "401" in row["last_sync_message"]
    assert "tok-123" not in caplog.text
    assert "tok-123" not in row["last_sync_message"]


def test_a_redirect_is_not_followed(_isolated_db) -> None:
    conn = _isolated_db
    endpoint_id = _endpoint(conn)
    _add(conn, _job("1"))
    receiver = Receiver(lambda req, n: httpx.Response(
        302, headers={"location": "https://elsewhere.test/steal"}))
    # Even a client configured to follow redirects must not follow this one.
    client = httpx.Client(transport=httpx.MockTransport(receiver.handler),
                          follow_redirects=True)
    result = endpoint_sync.sync(conn, client=client, sleep=lambda s: None)
    assert [str(r.url) for r in receiver.requests] == ["https://receiver.test/import"]
    assert result["failed"] == 1
    assert db.get_endpoint(conn, endpoint_id)["cursor_changed_at"] is None


def test_endpoints_keep_separate_cursors_and_one_failure_does_not_stop_others(
    _isolated_db,
) -> None:
    conn = _isolated_db
    good = _endpoint(conn, "good", "https://good.test/in")
    bad = _endpoint(conn, "bad", "https://bad.test/in")
    _add(conn, _job("1"))

    def respond(req, n):
        return httpx.Response(401) if "bad.test" in str(req.url) else ok_for(req)

    receiver = Receiver(respond)
    result = _run(conn, receiver)
    assert result["failed"] == 1
    assert receiver.sent_ids("good.test") == ["1"]
    assert db.get_endpoint(conn, good)["cursor_source_job_id"] == "1"
    assert db.get_endpoint(conn, bad)["cursor_source_job_id"] is None
    assert db.get_endpoint(conn, good)["last_sync_status"] == "ok"
    assert db.get_endpoint(conn, bad)["last_sync_status"] == "error"

    # The bad one recovers and catches up on its own; the good one only sees
    # what is new to it.
    _add(conn, _job("2"), seen_at=T0 + timedelta(minutes=1))
    after = Receiver()
    _run(conn, after)
    assert sorted(after.sent_ids("bad.test")) == ["1", "2"]
    assert after.sent_ids("good.test") == ["2"]


# --- batching ---------------------------------------------------------------


def test_batches_split_at_500_rows(_isolated_db) -> None:
    conn = _isolated_db
    _endpoint(conn)
    _add(conn, *[_job(f"r{i:03d}") for i in range(501)])
    receiver = Receiver()
    _run(conn, receiver)
    sizes = [len(b["rows"]) for b in receiver.bodies() if "announce" not in b]
    assert sizes == [500, 1]


def test_batches_split_by_size_and_an_oversized_row_is_skipped(
    _isolated_db, monkeypatch,
) -> None:
    assert endpoint_sync._MAX_BODY_BYTES == 4 * 1024 * 1024
    monkeypatch.setattr(endpoint_sync, "_MAX_BODY_BYTES", 6000)
    conn = _isolated_db
    endpoint_id = _endpoint(conn)
    _add(conn, *[_job(f"r{i}", description_text="x" * 1500) for i in range(5)])
    _add(conn, _job("huge", description_text="y" * 10_000), seen_at=T0 + timedelta(minutes=1))
    _add(conn, _job("last"), seen_at=T0 + timedelta(minutes=2))
    receiver = Receiver()
    _run(conn, receiver)

    bodies = [b for b in receiver.bodies() if "announce" not in b]
    assert all(len(json.dumps(b).encode()) <= 6000 for b in bodies)
    assert len(bodies) > 1
    sent = [r["source_job_id"] for b in bodies for r in b["rows"]]
    assert "huge" not in sent
    assert sorted(sent) == sorted([f"r{i}" for i in range(5)] + ["last"])
    assert db.get_endpoint(conn, endpoint_id)["cursor_source_job_id"] == "last"


# --- dry run, announce, disabled, rejected, lock ------------------------------


def test_dry_run_makes_no_request_and_saves_no_cursor(_isolated_db) -> None:
    conn = _isolated_db
    endpoint_id = _endpoint(conn)
    _add(conn, _job("1"), _job("2"))
    receiver = Receiver()
    result = _run(conn, receiver, dry_run=True)
    assert receiver.requests == []
    assert result["endpoints"][0]["sent"] == 2
    row = db.get_endpoint(conn, endpoint_id)
    assert row["cursor_changed_at"] is None and row["last_sync_at"] is None


def test_announce_is_sent_once_with_run_totals(_isolated_db) -> None:
    conn = _isolated_db
    _endpoint(conn)
    _add(conn, *[_job(f"r{i:03d}") for i in range(501)])

    def respond(req, n):
        body = json.loads(req.content)
        if "announce" in body:
            return httpx.Response(200, json={})
        return ok_for(req, inserted=len(body["rows"]) - 1, updated=1, closed=0)

    receiver = Receiver(respond)
    _run(conn, receiver)
    announces = [b for b in receiver.bodies() if "announce" in b]
    assert announces == [{"rows": [], "announce": {
        "inserted": 499, "updated": 2, "reopened": 0, "closed": 0}}]
    assert "announce" in receiver.bodies()[-1]


def test_no_announce_when_nothing_changed(_isolated_db) -> None:
    conn = _isolated_db
    _endpoint(conn)
    _add(conn, _job("1"))
    receiver = Receiver(lambda req, n: httpx.Response(200, json={"skipped": 1}))
    _run(conn, receiver)
    assert len(receiver.requests) == 1
    assert "announce" not in receiver.bodies()[0]


def test_disabled_endpoints_are_skipped(_isolated_db) -> None:
    conn = _isolated_db
    _endpoint(conn, enabled=False)
    _add(conn, _job("1"))
    receiver = Receiver()
    result = _run(conn, receiver)
    assert receiver.requests == [] and result["endpoints"] == []


def test_endpoint_filter_syncs_only_the_named_one(_isolated_db) -> None:
    conn = _isolated_db
    _endpoint(conn, "a", "https://a.test/in")
    _endpoint(conn, "b", "https://b.test/in")
    _add(conn, _job("1"))
    receiver = Receiver()
    _run(conn, receiver, endpoint_name="b")
    assert {r.url.host for r in receiver.requests} == {"b.test"}


def test_rejected_rows_are_logged_and_not_resent(_isolated_db) -> None:
    conn = _isolated_db
    endpoint_id = _endpoint(conn)
    _add(conn, _job("1"), _job("2"))
    run_id = db.start_run(conn, "test", "endpoint-sync")

    def respond(req, n):
        return ok_for(req, inserted=1, rejected=[
            {"index": 1, "externalId": "greenhouse:2", "message": "missing salary"}])

    _run(conn, Receiver(respond), run_id=run_id)
    events = conn.execute(
        "SELECT message, level FROM run_events WHERE run_id = ?", (run_id,)
    ).fetchall()
    assert any("1 rejected" in e["message"] and "missing salary" in e["message"]
               for e in events)
    assert db.get_endpoint(conn, endpoint_id)["cursor_source_job_id"] == "2"
    again = Receiver()
    _run(conn, again)
    assert again.requests == []


def test_sync_skips_while_another_session_holds_the_lock(_isolated_db) -> None:
    from sqlalchemy import create_engine

    from jobscrape import config

    other_engine = create_engine(config.TEST_DATABASE_URL)
    other = other_engine.connect()
    assert other.exec_driver_sql(
        f"SELECT pg_try_advisory_lock({endpoint_sync._LOCK_KEY})").scalar()
    try:
        with pytest.raises(endpoint_sync.EndpointSyncLocked):
            _run(_isolated_db, Receiver())
    finally:
        other.exec_driver_sql(f"SELECT pg_advisory_unlock({endpoint_sync._LOCK_KEY})")
        other.close()
        other_engine.dispose()
