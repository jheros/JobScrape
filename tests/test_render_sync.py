"""Tests for the pure mapping functions in render_sync.

`sync()` itself needs a real second database to write into and is exercised
live against the actual Render database instead (see docs/DOCKER.md-style
manual verification) — these cover the translation logic that doesn't need
either database: the dedup_key rule, the skills-array split, and the full
row mapping.
"""

from __future__ import annotations

from jobscrape.render_sync import _dedup_key, _skills_array, _to_render_row


def test_dedup_key_prefers_external_job_id():
    assert _dedup_key("greenhouse", "123", "https://x/1", "Engineer", "Acme") \
        == "greenhouse:123"


def test_dedup_key_falls_back_to_jd_url():
    assert _dedup_key("greenhouse", None, "https://x/1", "Engineer", "Acme") \
        == "https://x/1"


def test_dedup_key_falls_back_to_source_title_company():
    assert _dedup_key("greenhouse", None, None, "Engineer", "Acme") \
        == "greenhouse:Engineer:Acme"


def test_skills_array_splits_and_strips():
    assert _skills_array("python, django , postgres") == ["python", "django", "postgres"]


def test_skills_array_drops_blank_segments():
    assert _skills_array("python,,  ,django") == ["python", "django"]


def test_skills_array_empty_or_none():
    assert _skills_array("") == []
    assert _skills_array(None) == []


def test_to_render_row_maps_every_field():
    row = {
        "source": "greenhouse", "source_job_id": "123",
        "title": "Backend Engineer", "company": "Acme",
        "location_raw": "Remote - US",
        "salary_min": 120000.0, "salary_max": 180000.6,
        "apply_url": "https://boards.greenhouse.io/acme/jobs/123",
        "job_url": "https://boards.greenhouse.io/acme/jobs/123",
        "description_text": "Build things.",
        "posted_date": "2026-07-01",
        "first_seen_at": "2026-07-01T00:00:00+00:00",
        "skills": "python,django",
        "department": "Backend Engineering",
        "seniority": "senior",
    }
    out = _to_render_row(row)
    assert out == {
        "external_job_id": "123",
        "source_platform": "greenhouse",
        "dedup_key": "greenhouse:123",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Remote - US",
        "salary_min": 120000,
        "salary_max": 180001,
        "jd_url": "https://boards.greenhouse.io/acme/jobs/123",
        "jd_text": "Build things.",
        "posted_date": "2026-07-01",
        "created_at": "2026-07-01T00:00:00+00:00",
        "skills": ["python", "django"],
        "domain": "Backend Engineering",
        "level": "senior",
    }


def test_to_render_row_falls_back_to_job_url_when_no_apply_url():
    row = {
        "source": "lever", "source_job_id": "9", "title": "Engineer",
        "company": "Beta", "location_raw": None, "salary_min": None,
        "salary_max": None, "apply_url": None,
        "job_url": "https://jobs.lever.co/beta/9",
        "description_text": None, "posted_date": None,
        "first_seen_at": None, "skills": "", "department": None,
        "seniority": None,
    }
    out = _to_render_row(row)
    assert out["jd_url"] == "https://jobs.lever.co/beta/9"
    assert out["skills"] == []
    assert out["salary_min"] is None
    assert out["salary_max"] is None
