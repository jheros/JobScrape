"""The dashboard's HTTP layer.

`tests/test_dashboard.py` covers the queries "without starting a server", which
is fast and right for the SQL — but it means nothing here exercised request
validation, and that is exactly where the Jobs page broke.

A browser submits **every** named field in a form, including ones left blank.
The Min salary input therefore arrived as ``min_salary=`` on every press of
Apply, Pydantic could not parse ``""`` as a float, and FastAPI rejected the
whole request with a 422. The filters never ran; the page showed raw JSON. The
queries underneath were correct the entire time, which is why no query-level
test could have caught it.

These tests drive real requests, and the one that matters is
`test_apply_with_every_field_blank` — the literal URL a browser produces.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobscrape import db
from jobscrape.dashboard.app import app
from jobscrape.models import Job

#: What the Jobs filter form actually submits when nothing but the defaults are
#: touched. Written out in full, in form order, rather than tidied — a cleaned-up
#: version of this URL is precisely what would have hidden the bug.
BROWSER_SUBMIT = (
    "status=all&q=&source=&state=&workplace=&seniority=&skill="
    "&stale=&min_salary=&sort=newest"
)


@pytest.fixture()
def client(_isolated_db):
    """A client over the shared test transaction.

    `_isolated_db` (tests/conftest.py) monkeypatches `db.connect`, so the routes
    reach the same rolled-back transaction as the fixtures below.
    """
    db.upsert_jobs(_isolated_db, [
        Job(source="greenhouse", source_job_id="1", title="Staff Software Engineer",
            company="Acme", company_slug="acme", job_url="https://x/1",
            location_raw="Austin, TX", state="TX", posted_at="2026-07-01",
            salary_min=180_000, salary_max=220_000, salary_currency="USD",
            description_text="We use Python and Kubernetes."),
        Job(source="lever", source_job_id="2", title="Senior Backend Engineer",
            company="Globex", company_slug="globex", job_url="https://x/2",
            location_raw="Remote - US", posted_at="2026-07-02",
            description_text="Golang and Postgres."),
    ])
    with TestClient(app) as test_client:
        yield test_client


# --- the regression ---------------------------------------------------------


def test_apply_with_every_field_blank(client) -> None:
    """Pressing Apply without touching Min salary must return the page.

    This is the bug: `min_salary=` is unparseable as a float, so the request was
    rejected before any filtering happened.
    """
    response = client.get(f"/jobs?{BROWSER_SUBMIT}")
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "extra", ["seniority=staff", "skill=python", "workplace=remote",
              "state=TX", "source=greenhouse", "q=kubernetes", "sort=salary"],
)
def test_apply_with_each_filter_set(client, extra: str) -> None:
    """Every filter, submitted the way the form submits it — blanks and all."""
    response = client.get(f"/jobs?{BROWSER_SUBMIT}&{extra}")
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("field", ["min_salary", "page"])
def test_blank_numeric_parameters_are_treated_as_absent(client, field: str) -> None:
    assert client.get(f"/jobs?{field}=").status_code == 200


def test_a_real_number_still_filters(client) -> None:
    """The fix must not turn into "stop validating"."""
    assert client.get("/jobs?min_salary=1").status_code == 200
    assert "Staff Software Engineer" in client.get("/jobs?min_salary=1&status=all").text
    # 180k role excluded by a 300k floor.
    assert "Staff Software Engineer" not in client.get(
        "/jobs?min_salary=300000&status=all").text


def test_an_unparseable_number_is_still_rejected(client) -> None:
    """Blank means "not supplied"; garbage is still a client error."""
    assert client.get("/jobs?min_salary=abc").status_code == 422


# --- filters actually filter ------------------------------------------------


def test_filters_narrow_the_result_set(client) -> None:
    both = client.get("/jobs?status=all").text
    assert "Staff Software Engineer" in both and "Senior Backend Engineer" in both

    staff_only = client.get("/jobs?status=all&seniority=staff").text
    assert "Staff Software Engineer" in staff_only
    assert "Senior Backend Engineer" not in staff_only


def test_an_unmatchable_filter_renders_the_empty_state(client) -> None:
    """Not a 500, and not a silently unfiltered page."""
    response = client.get("/jobs?status=all&skill=nosuchskill")
    assert response.status_code == 200
    assert "Nothing matches these filters" in response.text


def test_a_skill_filter_matches_whole_entries_not_substrings(client) -> None:
    """`skills` is stored comma-separated, so the filter matches a whole entry.

    A padded LIKE would let "go" hit "django" and "mongodb", and a bare
    substring "o" would match nearly everything.
    """
    assert "Senior Backend Engineer" in client.get("/jobs?status=all&skill=go").text
    assert client.get("/jobs?status=all&skill=o").text.count('class="job-title"') == 0
    assert client.get("/jobs?status=all&skill=post").text.count('class="job-title"') == 0


# --- pagination -------------------------------------------------------------


def test_pagination_links_keep_the_filters(client) -> None:
    """The previous implementation rebuilt these with a string replace, which
    emitted a stray `&&` and would mangle a filter value containing `page=`."""
    response = client.get("/jobs?status=all&seniority=staff&min_salary=1&page=1")
    assert response.status_code == 200
    assert "&&" not in response.text


# --- every page still renders ----------------------------------------------


@pytest.mark.parametrize(
    "path", ["/", "/jobs", "/runs", "/coverage", "/sources", "/health",
             "/schedule", "/settings"],
)
def test_pages_render(client, path: str) -> None:
    assert client.get(path).status_code == 200


def test_health_reports_real_dependency_state(client) -> None:
    payload = client.get("/health").json()
    assert "healthy" in payload
    assert "database" in payload["checks"]


# --- fetch date range -------------------------------------------------------
#
# Only the validation-rejection paths are exercised here — none of these ever
# reach `manager.start()`, so no subprocess is spawned. The accepted-window
# case (argv actually built right) is a `RunManager`-level test instead
# (tests/test_dashboard.py), the same way every other `RunManager` behaviour
# is tested without going through a real HTTP request.


def test_fetch_start_rejects_an_unparseable_date(client) -> None:
    response = client.post("/api/runs/fetch/start", data={"since": "not-a-date"})
    assert response.status_code == 400


def test_fetch_start_rejects_since_after_until(client) -> None:
    response = client.post("/api/runs/fetch/start", data={
        "since": "2026-08-01", "until": "2026-07-01",
    })
    assert response.status_code == 400


def test_a_window_against_a_non_window_stage_is_rejected(client) -> None:
    response = client.post("/api/runs/discover/start", data={"since": "2026-08-01"})
    assert response.status_code == 400


def test_export_is_also_a_window_stage(client) -> None:
    """`export` also declares `add_window()` — confirms `_WINDOW_STAGES` isn't
    hard-coded to just `fetch`, without actually starting a real export."""
    from jobscrape.dashboard.app import _WINDOW_STAGES
    assert {"fetch", "export"} <= _WINDOW_STAGES


# --- schedule -----------------------------------------------------------


def test_create_update_toggle_delete_schedule(client) -> None:
    create = client.post("/api/schedule", data={
        "name": "Nightly", "stage": "fetch", "extra": "",
        "cron_minute": "0", "cron_hour": "7", "cron_day_of_week": "mon-sat",
        "timezone": "Asia/Bangkok",
    })
    assert create.status_code == 200, create.text
    schedule_id = create.json()["id"]
    assert "Nightly" in client.get("/schedule").text

    update = client.post(f"/api/schedule/{schedule_id}", data={
        "name": "Nightly (renamed)", "stage": "fetch", "extra": "",
        "cron_minute": "30", "cron_hour": "8", "cron_day_of_week": "*",
        "timezone": "Asia/Bangkok",
    })
    assert update.status_code == 200, update.text
    assert "Nightly (renamed)" in client.get("/schedule").text

    toggle = client.post(f"/api/schedule/{schedule_id}/toggle")
    assert toggle.status_code == 200

    delete = client.post(f"/api/schedule/{schedule_id}/delete")
    assert delete.status_code == 200
    assert "Nightly (renamed)" not in client.get("/schedule").text


def test_create_schedule_rejects_an_unknown_stage(client) -> None:
    response = client.post("/api/schedule", data={
        "name": "Bad", "stage": "rm -rf /", "cron_minute": "0", "cron_hour": "7",
        "cron_day_of_week": "*", "timezone": "Asia/Bangkok",
    })
    assert response.status_code == 400


def test_run_now_for_a_missing_schedule_is_404(client) -> None:
    assert client.post("/api/schedule/999999/run-now").status_code == 404


# --- settings -------------------------------------------------------------


def test_settings_save_writes_env_and_updates_process_environ(client, tmp_path, monkeypatch) -> None:
    import os

    from jobscrape.dashboard import envfile

    env_path = tmp_path / ".env"
    env_path.write_text("RAPIDAPI_KEY=old\n")
    monkeypatch.setattr(envfile, "ENV_PATH", env_path)

    response = client.post("/api/settings", data={"RAPIDAPI_KEY": "new-value"})
    assert response.status_code == 200
    assert response.json()["changed"] == ["RAPIDAPI_KEY"]
    assert "RAPIDAPI_KEY=new-value" in env_path.read_text()
    assert os.environ["RAPIDAPI_KEY"] == "new-value"


def test_settings_save_ignores_unknown_keys(client, tmp_path, monkeypatch) -> None:
    from jobscrape.dashboard import envfile

    env_path = tmp_path / ".env"
    monkeypatch.setattr(envfile, "ENV_PATH", env_path)

    response = client.post("/api/settings", data={"NOT_A_REAL_SETTING": "x"})
    assert response.status_code == 200
    assert response.json()["changed"] == []
