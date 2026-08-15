"""ai_check.py: prompt building, response parsing, the Ollama call, and the
DB round-trip contract that it must never touch the rule-based fields."""

from __future__ import annotations

import json

import httpx

from jobscrape import ai_check, db
from jobscrape.models import Job


def _job_dict(**over: object) -> dict:
    base = dict(
        id=1, title="Senior Backend Engineer", company="Acme",
        location_raw="Remote, USA",
        description_text="Must have an active TS/SCI security clearance. "
                          "Remote, US only. Full-time.",
        workplace_type="remote", seniority="senior", employment_type="full_time",
    )
    base.update(over)
    return base


def _job(job_id: str, **kw: object) -> Job:
    base = dict(
        source="ashby", source_job_id=job_id, title="Senior Backend Engineer",
        company="Acme", job_url=f"https://x/{job_id}",
    )
    base.update(kw)
    return Job(**base)  # type: ignore[arg-type]


# --- build_prompt -----------------------------------------------------------


def test_build_prompt_includes_the_posting_and_the_existing_hints() -> None:
    prompt = ai_check.build_prompt(_job_dict())
    assert "Senior Backend Engineer" in prompt
    assert "Acme" in prompt
    assert "Remote, USA" in prompt
    assert "TS/SCI security clearance" in prompt
    # The keyword classifier's own first-pass values are handed over as hints.
    assert "workplace_type: remote" in prompt
    assert "seniority: senior" in prompt
    assert "employment_type: full_time" in prompt


def test_build_prompt_truncates_a_huge_description() -> None:
    huge = "x" * (ai_check.MAX_DESCRIPTION_CHARS + 5000)
    prompt = ai_check.build_prompt(_job_dict(description_text=huge))
    assert len(prompt) < len(huge) + 2000


def test_build_prompt_tolerates_missing_fields() -> None:
    prompt = ai_check.build_prompt({"id": 1})
    assert "(none)" in prompt


# --- parse_response -----------------------------------------------------


def test_parse_response_valid_json_all_fields() -> None:
    text = json.dumps({
        "workplace_type": "remote", "requires_clearance": True, "is_us": True,
        "employment_type": "full_time", "seniority": "senior",
    })
    v = ai_check.parse_response(text)
    assert v == ai_check.Verdict(
        workplace_type="remote", requires_clearance=True, is_us=True,
        employment_type="full_time", seniority="senior",
    )
    assert not v.is_empty()


def test_parse_response_out_of_vocab_field_degrades_only_that_field() -> None:
    text = json.dumps({
        "workplace_type": "remote", "requires_clearance": False, "is_us": True,
        "employment_type": "weird-value", "seniority": "senior",
    })
    v = ai_check.parse_response(text)
    assert v.employment_type is None
    assert v.workplace_type == "remote"
    assert v.requires_clearance is False
    assert v.is_us is True
    assert v.seniority == "senior"


def test_parse_response_malformed_text_returns_empty_verdict_not_an_exception() -> None:
    v = ai_check.parse_response("this is not json at all")
    assert v.is_empty()


def test_parse_response_empty_string() -> None:
    assert ai_check.parse_response("").is_empty()
    assert ai_check.parse_response("   ").is_empty()


def test_parse_response_recovers_json_embedded_in_prose() -> None:
    text = (
        "Sure, here's the classification:\n"
        + json.dumps({
            "workplace_type": "onsite", "requires_clearance": True, "is_us": True,
            "employment_type": "contract", "seniority": "mid",
        })
        + "\nLet me know if you need anything else."
    )
    v = ai_check.parse_response(text)
    assert v.workplace_type == "onsite"
    assert v.requires_clearance is True
    assert v.seniority == "mid"


def test_parse_response_wrong_type_for_a_boolean_field_degrades_that_field() -> None:
    text = json.dumps({
        "workplace_type": "remote", "requires_clearance": "yes", "is_us": True,
        "employment_type": "full_time", "seniority": "senior",
    })
    v = ai_check.parse_response(text)
    assert v.requires_clearance is None
    assert v.workplace_type == "remote"


# --- classify_job (via httpx.MockTransport) ------------------------------


async def test_classify_job_returns_the_parsed_verdict_on_success() -> None:
    canned = {
        "response": json.dumps({
            "workplace_type": "remote", "requires_clearance": True, "is_us": True,
            "employment_type": "full_time", "seniority": "senior",
        }),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/generate"
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        return httpx.Response(200, json=canned)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        v = await ai_check.classify_job(
            client, "http://localhost:11434", "test-model", _job_dict(), timeout=5.0,
        )
    assert v.workplace_type == "remote"
    assert v.requires_clearance is True


async def test_classify_job_degrades_to_empty_verdict_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        v = await ai_check.classify_job(
            client, "http://localhost:11434", "test-model", _job_dict(), timeout=5.0,
        )
    assert v.is_empty()


async def test_classify_job_degrades_to_empty_verdict_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        v = await ai_check.classify_job(
            client, "http://localhost:11434", "test-model", _job_dict(), timeout=5.0,
        )
    assert v.is_empty()


async def test_classify_job_degrades_to_empty_verdict_on_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        v = await ai_check.classify_job(
            client, "http://localhost:11434", "test-model", _job_dict(), timeout=5.0,
        )
    assert v.is_empty()


# --- DB round-trip --------------------------------------------------------


def test_ai_check_candidates_returns_a_never_checked_open_job() -> None:
    with db.session() as conn:
        db.upsert_jobs(conn, [_job("1")])
        candidates = db.ai_check_candidates(conn, limit=10)
        assert any(c["title"] == "Senior Backend Engineer" for c in candidates)


def test_record_ai_check_removes_the_row_from_future_candidates() -> None:
    with db.session() as conn:
        db.upsert_jobs(conn, [_job("2")])
        candidates = db.ai_check_candidates(conn, limit=50)
        job_id = next(c["id"] for c in candidates if c["title"] == "Senior Backend Engineer")

        verdict = ai_check.Verdict(
            workplace_type="remote", requires_clearance=True, is_us=True,
            employment_type="full_time", seniority="senior",
        )
        db.record_ai_check(conn, job_id, verdict, "test-model")

        still_candidate = [
            c for c in db.ai_check_candidates(conn, limit=50) if c["id"] == job_id
        ]
        assert still_candidate == []

        row = conn.execute(
            "SELECT ai_workplace_type, ai_requires_clearance, ai_is_us, "
            "ai_employment_type, ai_seniority, ai_checked_at, ai_model "
            "FROM jobs WHERE id = ?", (job_id,),
        ).fetchall()[0]
        assert row["ai_workplace_type"] == "remote"
        assert row["ai_requires_clearance"] is True
        assert row["ai_is_us"] is True
        assert row["ai_employment_type"] == "full_time"
        assert row["ai_seniority"] == "senior"
        assert row["ai_checked_at"] is not None
        assert row["ai_model"] == "test-model"


def test_record_ai_check_never_touches_the_rule_based_fields() -> None:
    """The single most important regression test given the "never overwrite
    the rule-based fields" contract."""
    with db.session() as conn:
        db.upsert_jobs(conn, [_job(
            "3", workplace_type="onsite", seniority="staff",
            employment_type="contract",
        )])
        candidates = db.ai_check_candidates(conn, limit=50)
        job_id = next(c["id"] for c in candidates if c["title"] == "Senior Backend Engineer")

        before = conn.execute(
            "SELECT workplace_type, seniority, employment_type FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchall()[0]

        # A verdict that DISAGREES with the rule-based fields, to make sure
        # disagreement alone can't leak into the wrong columns.
        verdict = ai_check.Verdict(
            workplace_type="remote", requires_clearance=False, is_us=True,
            employment_type="full_time", seniority="entry",
        )
        db.record_ai_check(conn, job_id, verdict, "test-model")

        after = conn.execute(
            "SELECT workplace_type, seniority, employment_type FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchall()[0]
        assert after["workplace_type"] == before["workplace_type"] == "onsite"
        assert after["seniority"] == before["seniority"] == "staff"
        assert after["employment_type"] == before["employment_type"] == "contract"


# --- cmd_ai_check (CLI) ---------------------------------------------------


async def test_cmd_ai_check_dry_run_does_not_write(monkeypatch) -> None:
    from jobscrape import cli

    with db.session() as conn:
        db.upsert_jobs(conn, [_job("4")])

    async def fake_classify_job(client, base_url, model, job, *, timeout):
        return ai_check.Verdict(
            workplace_type="remote", requires_clearance=False, is_us=True,
            employment_type="full_time", seniority="senior",
        )

    monkeypatch.setattr(ai_check, "classify_job", fake_classify_job)

    args = cli.build_parser().parse_args(["ai-check", "--limit", "10", "--dry-run"])
    rc = await cli.cmd_ai_check(args)
    assert rc == 0

    with db.session() as conn:
        row = conn.execute(
            "SELECT ai_checked_at FROM jobs WHERE title = 'Senior Backend Engineer'"
        ).fetchall()
        assert all(r["ai_checked_at"] is None for r in row)


async def test_cmd_ai_check_writes_verdicts_when_not_dry_run(monkeypatch) -> None:
    from jobscrape import cli, config

    with db.session() as conn:
        db.upsert_jobs(conn, [_job("5")])

    async def fake_classify_job(client, base_url, model, job, *, timeout):
        return ai_check.Verdict(
            workplace_type="remote", requires_clearance=False, is_us=True,
            employment_type="full_time", seniority="senior",
        )

    monkeypatch.setattr(ai_check, "classify_job", fake_classify_job)

    args = cli.build_parser().parse_args(["ai-check", "--limit", "10"])
    rc = await cli.cmd_ai_check(args)
    assert rc == 0

    with db.session() as conn:
        row = conn.execute(
            "SELECT ai_checked_at, ai_model FROM jobs "
            "WHERE title = 'Senior Backend Engineer' AND source_job_id = '5'"
        ).fetchall()[0]
        assert row["ai_checked_at"] is not None
        assert row["ai_model"] == config.AI_CHECK_MODEL


async def test_cmd_ai_check_returns_zero_when_nothing_to_check() -> None:
    from jobscrape import cli

    with db.session() as conn:
        conn.execute("DELETE FROM jobs")

    args = cli.build_parser().parse_args(["ai-check", "--limit", "10"])
    rc = await cli.cmd_ai_check(args)
    assert rc == 0
