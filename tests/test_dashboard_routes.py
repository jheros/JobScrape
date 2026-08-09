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


@pytest.mark.parametrize(
    "extra",
    ["company=Acme", "employment_type=full-time", "department=Engineering",
     "industry=Software", "is_remote=true", "is_remote=false",
     "max_salary=200000", "posted_since=2026-01-01", "posted_until=2026-12-31",
     "scam_signals=true", "include_duplicates=true", "sync_state=pending"],
)
def test_apply_with_each_new_detailed_filter(client, extra: str) -> None:
    response = client.get(f"/jobs?{BROWSER_SUBMIT}&{extra}")
    assert response.status_code == 200, response.text


def test_the_company_filter_narrows_the_result_set(client) -> None:
    both = client.get("/jobs?status=all").text
    assert "Acme" in both and "Globex" in both

    acme_only = client.get("/jobs?status=all&company=Acme").text
    assert "Acme" in acme_only
    assert "Globex" not in acme_only


def test_an_unparseable_posted_date_degrades_to_no_filter(client) -> None:
    """Leaving a date box blank is the normal case, same reasoning
    `_blank_to_none` already applies to number inputs -- a garbage date
    must not 422 the whole page either."""
    response = client.get("/jobs?status=all&posted_since=not-a-date")
    assert response.status_code == 200
    assert "Acme" in response.text  # filter was silently dropped, not applied


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


@pytest.mark.parametrize("size", [25, 50, 100, 200])
def test_per_page_accepts_every_offered_size(client, size: int) -> None:
    response = client.get(f"/jobs?status=all&per_page={size}")
    assert response.status_code == 200
    assert f'value="{size}" selected' in response.text


def test_per_page_outside_the_allowlist_falls_back_to_the_default(client) -> None:
    """An arbitrary `per_page` (a hand-edited URL) must not turn into an
    unbounded query -- only the offered sizes are honoured."""
    response = client.get("/jobs?status=all&per_page=99999")
    assert response.status_code == 200
    assert 'value="50" selected' in response.text


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


# --- a real (non-dry-run) purge needs explicit confirmation ------------------
#
# The dashboard has no login on any route (docs/DOCKER.md) -- the client-side
# confirm() dialog on the "delete for real" button is a UX nicety a direct
# POST skips entirely. `manager.start` is monkeypatched here so these tests
# never actually spawn a subprocess -- the same reasoning the module
# docstring gives for testing only the rejection paths through HTTP.


def test_a_real_purge_without_confirmation_is_rejected(client, monkeypatch) -> None:
    from jobscrape.dashboard import app as app_module

    async def _boom(*a, **kw):
        raise AssertionError("manager.start must not be reached without confirmation")

    monkeypatch.setattr(app_module.manager, "start", _boom)

    response = client.post("/api/runs/purge/start", data={"extra": ""})
    assert response.status_code == 400


def test_a_real_purge_with_confirmation_is_allowed_through(client, monkeypatch) -> None:
    from jobscrape.dashboard import app as app_module

    started = {}

    async def _fake_start(stage, extra, window=None):
        started["stage"] = stage
        started["extra"] = extra
        return {"run_id": 1, "stage": stage}

    monkeypatch.setattr(app_module.manager, "start", _fake_start)

    response = client.post(
        "/api/runs/purge/start",
        data={"extra": "", "confirm": app_module._PURGE_CONFIRM_TOKEN},
    )
    assert response.status_code == 200
    assert started["stage"] == "purge"


def test_a_dry_run_purge_needs_no_confirmation(client, monkeypatch) -> None:
    from jobscrape.dashboard import app as app_module

    started = {}

    async def _fake_start(stage, extra, window=None):
        started["extra"] = extra
        return {"run_id": 1, "stage": stage}

    monkeypatch.setattr(app_module.manager, "start", _fake_start)

    response = client.post("/api/runs/purge/start", data={"extra": "--dry-run"})
    assert response.status_code == 200
    assert started["extra"] == ["--dry-run"]


def test_a_wrong_confirmation_token_is_still_rejected(client, monkeypatch) -> None:
    from jobscrape.dashboard import app as app_module

    async def _boom(*a, **kw):
        raise AssertionError("manager.start must not be reached with the wrong token")

    monkeypatch.setattr(app_module.manager, "start", _boom)

    response = client.post(
        "/api/runs/purge/start", data={"extra": "", "confirm": "yes please"},
    )
    assert response.status_code == 400


def test_apify_pull_and_jobright_fetch_are_also_window_stages(client) -> None:
    """The dashboard's "Fetch Apify jobs" and "Fetch JobRight jobs" cards
    each target their own bare stage directly and need a date range too."""
    from jobscrape.dashboard.app import _WINDOW_STAGES
    assert {"apify-pull", "jobright-fetch"} <= _WINDOW_STAGES


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


def test_describe_cron_renders_every_cadence_shape() -> None:
    from jobscrape.dashboard.app import describe_cron

    assert describe_cron("7", "0", "*", None) == "07:00"
    assert describe_cron("9", "30", "mon-fri", None) == "09:30 (mon-fri)"
    assert describe_cron("*", "*/15", "*", None) == "every 15 min"
    assert describe_cron("7", "0", "*", 2) == "every 2 days at 07:00"
    # A stored hour-step value predating the every-N-minutes UI (e.g.
    # fetch-workday's "*/2", created directly via the API) must render
    # readably, not crash `|int` the way the old template filter would.
    assert describe_cron("*/2", "0", "*", None) == "every 2h at :00"


def test_create_schedule_with_interval_days_round_trips(client) -> None:
    create = client.post("/api/schedule", data={
        "name": "Every 2 days", "stage": "discover-validate", "extra": "",
        "cron_minute": "0", "cron_hour": "7", "cron_day_of_week": "*",
        "timezone": "Asia/Bangkok", "interval_days": "2",
    })
    assert create.status_code == 200, create.text
    schedule_id = create.json()["id"]

    with db.session() as conn:
        row = db.get_schedule(conn, schedule_id)
    assert row["interval_days"] == 2
    assert "every 2 days" in client.get("/schedule").text

    # Switching back to a plain cron schedule must clear interval_days, not
    # just leave it stale alongside the new cron fields.
    update = client.post(f"/api/schedule/{schedule_id}", data={
        "name": "Every 2 days", "stage": "discover-validate", "extra": "",
        "cron_minute": "0", "cron_hour": "7", "cron_day_of_week": "*",
        "timezone": "Asia/Bangkok", "interval_days": "",
    })
    assert update.status_code == 200, update.text
    with db.session() as conn:
        row = db.get_schedule(conn, schedule_id)
    assert row["interval_days"] is None


def test_create_schedule_rejects_an_unknown_stage(client) -> None:
    response = client.post("/api/schedule", data={
        "name": "Bad", "stage": "rm -rf /", "cron_minute": "0", "cron_hour": "7",
        "cron_day_of_week": "*", "timezone": "Asia/Bangkok",
    })
    assert response.status_code == 400


def test_run_now_for_a_missing_schedule_is_404(client) -> None:
    assert client.post("/api/schedule/999999/run-now").status_code == 404


def test_run_now_resolves_a_relative_window_preset(client, monkeypatch) -> None:
    """A schedule stored with a `RELATIVE_WINDOWS` preset name (e.g.
    "fetch-1w") isn't a real `STAGES` key — "Run now" used to pass it to
    `manager.start()` unresolved, which rejected it outright as unknown."""
    from datetime import date, timedelta

    from jobscrape.dashboard.runner import manager

    create = client.post("/api/schedule", data={
        "name": "Weekly fetch", "stage": "fetch-1w", "extra": "",
        "cron_minute": "0", "cron_hour": "7", "cron_day_of_week": "*",
        "timezone": "Asia/Bangkok",
    })
    assert create.status_code == 200, create.text
    schedule_id = create.json()["id"]

    seen = {}

    async def fake_start(stage, extra=None, window=None):
        seen["stage"] = stage
        seen["window"] = window
        return {"run_id": 1, "stage": stage, "pid": 1}

    monkeypatch.setattr(manager, "start", fake_start)
    response = client.post(f"/api/schedule/{schedule_id}/run-now")
    assert response.status_code == 200, response.text

    assert seen["stage"] == "fetch"
    since, until = seen["window"]
    assert since == date.today() - timedelta(days=7)
    assert until is None


def test_manual_stage_start_triggers_a_render_sync_check(client, monkeypatch) -> None:
    """render-sync used to fire only from the scheduler's own cron trigger
    (`scheduler._fire()`) — clicking a stage directly from the Runs page (or
    "Run now" on the Schedule tab) went through `app.py` instead, which never
    called it. Both manual paths must call `maybe_sync_after` too, exactly
    like a schedule firing does, or a manual apify-pull never reaches
    Render until something else happens to sync it."""
    from jobscrape.dashboard import app as app_module
    from jobscrape.dashboard.runner import manager

    async def fake_start(stage, extra=None, window=None):
        return {"run_id": 1, "stage": stage, "pid": 1}

    seen = []
    monkeypatch.setattr(manager, "start", fake_start)
    monkeypatch.setattr(app_module.scheduler_module, "maybe_sync_after", seen.append)

    response = client.post("/api/runs/apify-pull/start")
    assert response.status_code == 200, response.text
    assert seen == ["apify-pull"]


def test_run_now_also_triggers_a_render_sync_check(client, monkeypatch) -> None:
    from jobscrape.dashboard import app as app_module
    from jobscrape.dashboard.runner import manager

    create = client.post("/api/schedule", data={
        "name": "Nightly apify", "stage": "apify-pull", "extra": "",
        "cron_minute": "0", "cron_hour": "7", "cron_day_of_week": "*",
        "timezone": "Asia/Bangkok",
    })
    assert create.status_code == 200, create.text
    schedule_id = create.json()["id"]

    async def fake_start(stage, extra=None, window=None):
        return {"run_id": 1, "stage": stage, "pid": 1}

    seen = []
    monkeypatch.setattr(manager, "start", fake_start)
    monkeypatch.setattr(app_module.scheduler_module, "maybe_sync_after", seen.append)

    response = client.post(f"/api/schedule/{schedule_id}/run-now")
    assert response.status_code == 200, response.text
    assert seen == ["apify-pull"]


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


def test_settings_save_does_not_write_fields_the_user_never_touched(
    client, tmp_path, monkeypatch,
) -> None:
    """The actual bug: a browser submits EVERY field on the Settings page,
    including ones left blank because they were never in `.env` at all
    (`APIFY_INDEED_ACTOR` has a non-empty built-in default, so a blank
    submission for it must not get written — that's what silently broke
    apify-pull in practice, since a written blank overrides that default)."""
    from jobscrape.dashboard import envfile

    env_path = tmp_path / ".env"
    env_path.write_text("RAPIDAPI_KEY=old\n")
    monkeypatch.setattr(envfile, "ENV_PATH", env_path)

    response = client.post("/api/settings", data={
        "RAPIDAPI_KEY": "new-value",
        "APIFY_INDEED_ACTOR": "",  # untouched — blank only because it was never set
    })
    assert response.status_code == 200
    assert response.json()["changed"] == ["RAPIDAPI_KEY"]
    assert "APIFY_INDEED_ACTOR" not in env_path.read_text()


def test_settings_renders_the_current_blocked_companies_as_chips(
    client, tmp_path, monkeypatch,
) -> None:
    """The "currently registered blocked companies" list is satisfied by the
    initial chip render itself -- whatever's in .env right now IS the list
    shown, no separate query."""
    from jobscrape.dashboard import envfile

    env_path = tmp_path / ".env"
    env_path.write_text("JOBSCRAPE_BLOCKED_COMPANIES=Amazon,Apple Roofing\n")
    monkeypatch.setattr(envfile, "ENV_PATH", env_path)

    page = client.get("/settings").text
    assert 'data-company="Amazon"' in page
    assert 'data-company="Apple Roofing"' in page


def test_settings_save_writes_blocked_companies_as_one_comma_joined_value(
    client, tmp_path, monkeypatch,
) -> None:
    """The chip UI is a client-side concern only -- the backend contract is
    unchanged, still one comma-separated value under the same form key."""
    from jobscrape.dashboard import envfile

    env_path = tmp_path / ".env"
    monkeypatch.setattr(envfile, "ENV_PATH", env_path)

    response = client.post("/api/settings", data={
        "JOBSCRAPE_BLOCKED_COMPANIES": "Amazon,Apple Roofing,Stripe",
    })
    assert response.status_code == 200
    assert response.json()["changed"] == ["JOBSCRAPE_BLOCKED_COMPANIES"]
    assert "JOBSCRAPE_BLOCKED_COMPANIES=Amazon,Apple Roofing,Stripe" in env_path.read_text()


def test_settings_save_rejects_a_newline_injection_attempt(client, tmp_path, monkeypatch) -> None:
    """The dashboard has no login on any route (docs/DOCKER.md). A value with
    an embedded newline would otherwise write a second, unrelated `KEY=value`
    line into `.env` -- bypassing `ALL_KEYS`'s allowlist on which fields this
    form may write, since that allowlist only checks the field NAME. Must be
    rejected before anything reaches disk, and must not touch os.environ
    either (the loop that applies `changed` only runs after a successful
    write)."""
    import os

    from jobscrape.dashboard import envfile

    env_path = tmp_path / ".env"
    env_path.write_text("RAPIDAPI_KEY=old\n")
    monkeypatch.setattr(envfile, "ENV_PATH", env_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    response = client.post("/api/settings", data={
        "JOBSCRAPE_CONTACT": "me@example.com\nDATABASE_URL=postgresql://evil/db",
    })
    assert response.status_code == 400
    assert "DATABASE_URL" not in env_path.read_text()
    assert "DATABASE_URL" not in os.environ


# --- admin session: view-only for everyone except the configured passcode --
#
# Every other test in this file runs with `JOBSCRAPE_ADMIN_PASSCODE` unset
# (the default in this environment), which is itself the regression case for
# "an existing deployment that never configures this must keep working
# exactly as before" -- confirmed here explicitly, not just implicitly by
# every other test in the file passing with no cookie at all.


def test_with_no_passcode_configured_every_visitor_may_still_mutate(client) -> None:
    response = client.post("/api/schedule", data={
        "name": "no passcode set", "stage": "validate", "cron_minute": "0",
        "cron_hour": "7", "cron_day_of_week": "*", "timezone": "Asia/Bangkok",
    })
    assert response.status_code == 200, response.text


def test_admin_login_with_the_right_passcode_sets_a_cookie(client, monkeypatch) -> None:
    from jobscrape import config
    from jobscrape.dashboard import auth

    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    response = client.post(
        "/admin-login", data={"passcode": "hunter2"}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert auth.COOKIE_NAME in response.cookies


def test_admin_login_with_the_wrong_passcode_sets_no_cookie(client, monkeypatch) -> None:
    from jobscrape import config
    from jobscrape.dashboard import auth

    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    response = client.post(
        "/admin-login", data={"passcode": "wrong"}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=1" in response.headers["location"]
    assert auth.COOKIE_NAME not in response.cookies


def test_admin_logout_expires_the_cookie(client, monkeypatch) -> None:
    from jobscrape import config
    from jobscrape.dashboard import auth

    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    response = client.post("/admin-logout", follow_redirects=False)
    assert response.status_code == 303
    # Starlette's delete_cookie replaces the value with one that expires
    # immediately rather than omitting the header -- either way the client's
    # own jar must not carry a usable cookie afterward.
    client.cookies.extract_cookies(response)
    assert not client.cookies.get(auth.COOKIE_NAME)


def test_the_whole_mutating_surface_refuses_a_non_admin_visitor(
    client, monkeypatch,
) -> None:
    """One pass over every admin-gated route, not one test per route: build
    everything AS admin first (a schedule to toggle/edit/delete/run), then
    confirm each one refuses a plain visitor once a passcode is configured."""
    from jobscrape import config
    from jobscrape.dashboard import auth

    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    client.cookies.set(auth.COOKIE_NAME, auth.make_token())

    create = client.post("/api/schedule", data={
        "name": "gate-test", "stage": "validate", "cron_minute": "0",
        "cron_hour": "7", "cron_day_of_week": "*", "timezone": "Asia/Bangkok",
    })
    assert create.status_code == 200, create.text
    schedule_id = create.json()["id"]

    client.cookies.delete(auth.COOKIE_NAME)  # now a plain, non-admin visitor

    sched_payload = {
        "name": "gate-test", "stage": "validate", "cron_minute": "0",
        "cron_hour": "7", "cron_day_of_week": "*", "timezone": "Asia/Bangkok",
    }
    denied = {
        "start a stage": client.post("/api/runs/validate/start"),
        "stop a stage": client.post("/api/runs/validate/stop"),
        "create a schedule": client.post("/api/schedule", data=sched_payload),
        "update a schedule": client.post(f"/api/schedule/{schedule_id}", data=sched_payload),
        "toggle a schedule": client.post(f"/api/schedule/{schedule_id}/toggle"),
        "run a schedule now": client.post(f"/api/schedule/{schedule_id}/run-now"),
        "delete a schedule": client.post(f"/api/schedule/{schedule_id}/delete"),
        "restart the dashboard": client.post("/api/settings/restart"),
    }
    for action, response in denied.items():
        assert response.status_code == 403, f"{action} should have been denied: {response.text}"


def test_a_confirmed_purge_still_needs_admin(client, monkeypatch) -> None:
    """The purge-confirmation token (see the earlier deep-review commit) and
    the admin gate are independent checks -- having the right token is not
    itself enough for a non-admin visitor."""
    from jobscrape import config
    from jobscrape.dashboard import app as app_module

    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    response = client.post("/api/runs/purge/start", data={
        "extra": "", "confirm": app_module._PURGE_CONFIRM_TOKEN,
    })
    assert response.status_code == 403


def test_a_non_admin_may_still_add_a_blocked_company(client, tmp_path, monkeypatch) -> None:
    """The one thing product wants every visitor able to do, even once a
    passcode locks down everything else."""
    from jobscrape import config
    from jobscrape.dashboard import envfile

    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    env_path = tmp_path / ".env"
    monkeypatch.setattr(envfile, "ENV_PATH", env_path)

    response = client.post("/api/settings", data={
        "JOBSCRAPE_BLOCKED_COMPANIES": "Acme, Globex",
    })
    assert response.status_code == 200, response.text
    assert response.json()["changed"] == ["JOBSCRAPE_BLOCKED_COMPANIES"]
    assert "JOBSCRAPE_BLOCKED_COMPANIES=Acme, Globex" in env_path.read_text()


def test_a_non_admin_cannot_smuggle_other_fields_alongside_blocked_companies(
    client, tmp_path, monkeypatch,
) -> None:
    """A raw POST can carry any field regardless of what the page shows --
    the server, not the form, is what must actually restrict this."""
    from jobscrape import config
    from jobscrape.dashboard import envfile

    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    env_path = tmp_path / ".env"
    env_path.write_text("RAPIDAPI_KEY=old\n")
    monkeypatch.setattr(envfile, "ENV_PATH", env_path)

    response = client.post("/api/settings", data={
        "JOBSCRAPE_BLOCKED_COMPANIES": "Acme",
        "RAPIDAPI_KEY": "stolen-key",
    })
    assert response.status_code == 200, response.text
    assert response.json()["changed"] == ["JOBSCRAPE_BLOCKED_COMPANIES"]
    assert "RAPIDAPI_KEY=old" in env_path.read_text()
    assert "stolen-key" not in env_path.read_text()


def test_pages_stay_viewable_for_a_non_admin_visitor(client, monkeypatch) -> None:
    """'Should see, not click' -- every page still renders (200), just with
    its controls blurred/disabled client-side; only the mutating endpoints
    themselves are actually gated."""
    from jobscrape import config

    monkeypatch.setattr(config, "ADMIN_PASSCODE", "hunter2")
    for path in ("/", "/jobs", "/runs", "/coverage", "/sources", "/schedule", "/settings"):
        response = client.get(path)
        assert response.status_code == 200, f"{path}: {response.status_code}"
