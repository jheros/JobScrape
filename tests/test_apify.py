"""Apify ingestion: mappers, the webhook receiver, and the ATS-link recovery.

The mapper tests run against recorded fixtures with no network and no token,
because the mapping is the part with real behavioural risk. The receiver tests
are mostly about what it *refuses* — it is the only route in this project meant
to be publicly reachable, and every other route is protected solely by binding
to localhost.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobscrape import config, db
from jobscrape.sources.apify import ats_link, indeed, linkedin, mapper_for

# --- recorded fixtures ------------------------------------------------------
#
# Shapes taken from each actor's published output schema. Every field the
# mappers read is present, and the nullable ones are exercised separately below.

INDEED_ITEM: dict[str, Any] = {
    "key": "a1b2c3",
    "title": "Senior Backend Engineer",
    "normalizedTitle": "senior backend engineer",
    "jobCategory": "Software Development",
    "jobUrl": "https://www.indeed.com/viewjob?jk=a1b2c3",
    "applyUrl": "https://boards.greenhouse.io/acmecorp/jobs/4021",
    "datePublished": "2026-07-30T10:00:00.000Z",
    "dateScraped": "2026-07-31T02:00:00.000Z",
    "location": {
        "streetAddress": None, "city": "Austin", "state": "TX",
        "country": "US", "postalCode": "78701",
    },
    "baseSalary_min": 165000,
    "baseSalary_max": 210000,
    "salary_period": "YEAR",
    "salary_currency": "USD",
    "description_text": "Build distributed services in Python and Go on Kubernetes.",
    "description_html": "<p>Build distributed services in Python and Go on Kubernetes.</p>",
    "company": {"companyName": "Acme Corp", "companyShortName": "Acme"},
    "attributes": ["Full-time", "Remote", "401(k)", "Health insurance"],
}

LINKEDIN_ITEM: dict[str, Any] = {
    "jobId": "3912847",
    "jobTitle": "Staff Platform Engineer",
    "location": "San Francisco, CA",
    "salaryInfo": ["$180,000/yr - $240,000/yr"],
    "postedTime": "2 days ago",
    "publishedAt": "2026-07-29T08:00:00.000Z",
    "jobUrl": "https://www.linkedin.com/jobs/view/3912847",
    "jobDescription": "<p>Terraform, Kubernetes and Go. Three days in the office.</p>",
    "companyName": "Globex Industries",
    "companyUrl": "https://www.linkedin.com/company/globex",
    "contractType": "Full-time",
    "experienceLevel": "Mid-Senior level",
    "workType": "Hybrid",
    "sector": "Software Development",
    "applyUrl": "https://jobs.lever.co/globex/9f2c",
}


# --- Indeed -----------------------------------------------------------------


def test_indeed_maps_every_field() -> None:
    job = indeed.to_job(INDEED_ITEM)
    assert job is not None
    assert job.source == "indeed"
    assert job.source_job_id == "a1b2c3"
    assert job.title == "Senior Backend Engineer"
    assert job.company == "Acme Corp"
    assert job.department == "Software Development"
    # Structured location, so `parse_us_location` is never consulted.
    assert (job.city, job.state, job.country) == ("Austin", "TX", "US")
    assert job.location_raw == "Austin, TX, US"
    assert job.salary_min == 165000 and job.salary_max == 210000
    assert job.salary_currency == "USD"
    assert job.posted_at is not None and job.posted_at.startswith("2026-07-30")
    assert "Kubernetes" in (job.description_text or "")


def test_indeed_reads_the_apply_url_for_the_board_slug() -> None:
    """The whole point of `ats_link`: cluster with the company's own board row.

    `Job.__post_init__` would otherwise slugify "Acme Corp" to `acme-corp`,
    which never matches Greenhouse's `acmecorp`.
    """
    job = indeed.to_job(INDEED_ITEM)
    assert job is not None and job.company_slug == "acmecorp"


def test_indeed_falls_back_to_the_company_name() -> None:
    """An Indeed-hosted apply flow names no board, and guessing one would be
    worse than not having it — a wrong slug clusters unrelated employers.

    The fallback is `Job.__post_init__`'s `slugify`, which already strips legal
    suffixes, so "Acme Corp" and a Greenhouse board called `acme` agree here
    without any help from `ats_link`.
    """
    item = {**INDEED_ITEM, "applyUrl": "https://www.indeed.com/apply/a1b2c3",
            "jobUrl": "https://www.indeed.com/viewjob?jk=a1b2c3"}
    job = indeed.to_job(item)
    assert job is not None and job.company_slug == "acme"

    # A name with no legal suffix keeps its separators, which is exactly the
    # case `dedupe`'s slug normalisation exists for.
    other = indeed.to_job({**item, "company": {"companyName": "10a Labs"}})
    assert other is not None and other.company_slug == "10a-labs"


def test_an_hourly_indeed_rate_is_annualised() -> None:
    """The 488-row bug, in the one place it could recur.

    Indeed publishes `salary_period: "HOUR"` beside the figures. Writing 60
    into the same column as 210000 is what once put hourly rates across 11
    sources into an annual field, with nothing to tell them apart afterwards.
    """
    item = {**INDEED_ITEM, "baseSalary_min": 60, "baseSalary_max": 80,
            "salary_period": "HOUR"}
    job = indeed.to_job(item)
    assert job is not None
    assert job.salary_min == 60 * 2080
    assert job.salary_max == 80 * 2080
    # The figures as published survive, so the conversion is auditable.
    assert job.salary_raw is not None and "HOUR" in job.salary_raw


def test_an_unrecognised_period_yields_no_salary() -> None:
    """Abstaining beats guessing annual: a wrong number is unrecoverable."""
    item = {**INDEED_ITEM, "baseSalary_min": 60, "baseSalary_max": 80,
            "salary_period": "PER WIDGET"}
    job = indeed.to_job(item)
    assert job is not None and job.salary_min is None and job.salary_max is None


def test_indeed_attributes_drive_workplace_and_employment_type() -> None:
    """`attributes` mixes schedule, arrangement and benefits in one flat list.

    Indeed renders these as chips on the posting, so they are far more reliable
    than the same words appearing in prose.
    """
    job = indeed.to_job(INDEED_ITEM)
    assert job is not None
    assert job.workplace_type == "remote"
    assert job.employment_type == "full_time"

    hybrid = indeed.to_job({**INDEED_ITEM, "attributes": ["Contract", "Hybrid work"]})
    assert hybrid is not None
    assert hybrid.workplace_type == "hybrid"
    assert hybrid.employment_type == "contract"


# --- LinkedIn ---------------------------------------------------------------


def test_linkedin_maps_every_field() -> None:
    job = linkedin.to_job(LINKEDIN_ITEM)
    assert job is not None
    assert job.source == "linkedin"
    assert job.source_job_id == "3912847"
    assert job.title == "Staff Platform Engineer"
    assert job.company == "Globex Industries"
    assert job.company_slug == "globex"          # from the Lever apply URL
    assert job.location_raw == "San Francisco, CA"
    # `sector` is the employer's MARKET, so it lands in `industry`. It used to
    # be written into `department`, which is the team a role sits in — two
    # different facets sharing one column. LinkedIn publishes no department.
    assert job.industry == "Software Development"
    assert job.department is None
    assert job.employment_type == "full_time"    # normalised from "Full-time"
    assert job.workplace_type == "hybrid"        # from workType, not the prose
    assert job.seniority == "staff"              # from the title, see below
    assert "Terraform" in (job.description_text or "")


def test_linkedin_salary_prose_is_parsed() -> None:
    """`salaryInfo` arrives as prose with a per-unit suffix.

    "$180,000/yr - $240,000/yr" matched nothing before `_PER_UNIT` was added to
    the range pattern — the suffix sat between the first amount and the dash,
    so every LinkedIn salary would have been dropped.
    """
    job = linkedin.to_job(LINKEDIN_ITEM)
    assert job is not None
    assert job.salary_min == 180000 and job.salary_max == 240000


def test_an_hourly_linkedin_rate_is_annualised() -> None:
    item = {**LINKEDIN_ITEM, "salaryInfo": ["$85.00/hr - $110.00/hr"]}
    job = linkedin.to_job(item)
    assert job is not None
    assert job.salary_min == 85 * 2080
    assert job.salary_max == 110 * 2080


def test_linkedin_experience_level_does_not_override_the_title() -> None:
    """LinkedIn's four buckets are coarser than the seniority ladder.

    "Mid-Senior level" covers mid, senior, staff and principal. The ladder
    reads "Staff Platform Engineer" as staff, and because `__post_init__` only
    fills a blank, passing the coarse value would win and be a downgrade.
    """
    job = linkedin.to_job(LINKEDIN_ITEM)
    assert job is not None
    assert job.seniority == "staff"
    assert LINKEDIN_ITEM["experienceLevel"] == "Mid-Senior level"


def test_easy_apply_skips_the_ats_parse() -> None:
    """`applyType` states outright what `ats_link` otherwise has to guess.

    An Easy Apply link stays on linkedin.com, so the parse is guaranteed to
    find no board — the slug falls back to the company name. EXTERNAL still
    resolves one, and a missing `applyType` still tries, since the field is
    nullable and declining would cost a real slug.
    """
    easy = linkedin.to_job({**LINKEDIN_ITEM, "applyType": "EASY_APPLY"})
    assert easy is not None and easy.company_slug == "globex-industries"

    external = linkedin.to_job({**LINKEDIN_ITEM, "applyType": "EXTERNAL"})
    assert external is not None and external.company_slug == "globex"

    assert "applyType" not in LINKEDIN_ITEM
    assert linkedin.to_job(LINKEDIN_ITEM).company_slug == "globex"


def test_linkedin_falls_back_to_the_company_industry() -> None:
    """`companyIndustry` only exists with `enrichCompanyData: true`, so it is
    the fallback rather than the primary."""
    item = {k: v for k, v in LINKEDIN_ITEM.items() if k != "sector"}
    job = linkedin.to_job({**item, "companyIndustry": "Hospitals and Health Care"})
    assert job is not None and job.industry == "Hospitals and Health Care"


@pytest.mark.parametrize(
    "item",
    [
        {}, {"jobId": "1"}, {"jobId": "1", "jobTitle": "X"},
        {"jobId": "1", "jobTitle": "X", "companyName": "Y"},   # no link
        {"jobTitle": "X", "companyName": "Y", "jobUrl": "https://x/1"},  # no id
    ],
)
def test_an_item_without_an_identity_is_dropped(item: dict[str, Any]) -> None:
    """Every field on this actor is nullable, so identity is checked."""
    assert linkedin.to_job(item) is None


def test_to_jobs_skips_what_it_cannot_map() -> None:
    jobs = linkedin.to_jobs([{}, LINKEDIN_ITEM, "not a dict", None])  # type: ignore[list-item]
    assert len(jobs) == 1


# --- ATS link recovery ------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://boards.greenhouse.io/acme/jobs/4021", ("greenhouse", "acme")),
        ("https://job-boards.greenhouse.io/acme/jobs/4021", ("greenhouse", "acme")),
        ("https://jobs.lever.co/acme/9f2c-aaa", ("lever", "acme")),
        ("https://jobs.ashbyhq.com/acme/abc-123", ("ashby", "acme")),
        ("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/x", ("workday", "acme")),
        ("https://acme.wd103.myworkdayjobs.com/careers", ("workday", "acme")),
        ("https://acme.breezy.hr/p/123", ("breezy", "acme")),
        ("https://acme.applytojob.com/apply/xyz", ("jazzhr", "acme")),
        ("https://jobs.smartrecruiters.com/Acme/744000", ("smartrecruiters", "Acme")),
        ("https://acme.teamtailor.com/jobs/123", ("teamtailor", "acme")),
        ("https://apply.workable.com/acme/j/ABC/", ("workable", "acme")),
    ],
)
def test_a_board_url_yields_its_slug(url: str, expected: tuple[str, str]) -> None:
    assert ats_link.board_slug(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.indeed.com/viewjob?jk=abc",     # the aggregator itself
        "https://www.linkedin.com/jobs/view/123",
        "https://acme.com/careers/engineer",         # a company's own site
        "https://www.myworkdayjobs.com/careers",     # no tenant label
        "https://jobs.lever.co/",                    # no slug in the path
        "not a url", "", None,
    ],
)
def test_a_non_board_url_yields_nothing(url: str | None) -> None:
    """Returning None is the honest answer; a guessed slug would cluster two
    unrelated employers' postings together."""
    assert ats_link.board_slug(url) is None


def test_slug_normalisation_collapses_punctuation() -> None:
    """`10a-labs` (aggregator) and `10alabs` (Greenhouse) are one company.

    Measured on the archive, normalising doubles how many aggregator rows match
    a known ATS company — adzuna 3% to 7%, jooble 8% to 14%.
    """
    assert ats_link.normalise_slug("10a-labs") == ats_link.normalise_slug("10alabs")
    assert ats_link.normalise_slug("Acme Corp.") == "acmecorp"
    assert ats_link.normalise_slug(None) is None
    assert ats_link.normalise_slug("") is None


def test_mapper_for_picks_by_actor_name() -> None:
    assert mapper_for("cheap_scraper~indeed-job-scraper") is indeed
    assert mapper_for("cheap_scraper~linkedin-job-scraper") is linkedin
    # An actor this app cannot read must not be guessed at.
    assert mapper_for("someone~glassdoor-scraper") is None
    assert mapper_for(None) is None


# --- resolving an actor by its opaque id ------------------------------------
#
# A run reports `actId` as `nlZZi3lZre4fM9IET`, never `user~name`. `mapper_for`
# matches on the name *containing* "indeed" or "linkedin", so it returned None
# for every real run and ingestion failed with "no mapper for actor" every
# single time. The suite was green because every fixture here was a name.


def _resolve(actor, *, name=None, configured=()):
    """`resolve_mapper` with the network stubbed and the cache cleared."""
    from jobscrape.sources.apify import _RESOLVED, client, resolve_mapper

    async def actor_name(_fetcher, _actor):
        return name

    original_lookup = client.actor_name
    original_config = (config.APIFY_INDEED_ACTOR, config.APIFY_LINKEDIN_ACTOR)
    client.actor_name = actor_name
    if configured:
        config.APIFY_INDEED_ACTOR, config.APIFY_LINKEDIN_ACTOR = configured
    _RESOLVED.clear()
    try:
        return asyncio.run(resolve_mapper(None, actor))
    finally:
        client.actor_name = original_lookup
        config.APIFY_INDEED_ACTOR, config.APIFY_LINKEDIN_ACTOR = original_config
        _RESOLVED.clear()


def test_an_opaque_actor_id_resolves_through_the_api() -> None:
    """The case that was broken in production and green in this file."""
    assert _resolve("nlZZi3lZre4fM9IET",
                    name="cheap_scraper~indeed-job-scraper") is indeed
    assert _resolve("2rJKkhh7vjpX7pvjg",
                    name="cheap_scraper~linkedin-job-scraper") is linkedin


def test_a_configured_actor_id_resolves_with_no_request() -> None:
    """`name=None` stands in for the API being unreachable: a user who put the
    id in `APIFY_INDEED_ACTOR` must not need it."""
    assert _resolve("nlZZi3lZre4fM9IET", name=None,
                    configured=("nlZZi3lZre4fM9IET", "")) is indeed


def test_a_readable_actor_name_still_resolves_without_a_request() -> None:
    assert _resolve("cheap_scraper~linkedin-job-scraper", name=None) is linkedin


def test_an_unknown_actor_resolves_to_nothing() -> None:
    """Guessing would write another actor's fields into these columns."""
    assert _resolve("aBcDeFgHiJkLmNoPq", name="someone~glassdoor-scraper") is None
    assert _resolve("", name="cheap_scraper~indeed-job-scraper") is None


# --- the mappers against the actors' PUBLISHED schemas -----------------------
#
# Both defects in this feature came from fixtures written from a prose summary
# of the actor's marketing page: the fixtures agreed with the mappers and
# neither agreed with the actor. These read the vendored schema instead —
# `tests/fixtures/apify/*-dataset-schema.json`, straight from
# `GET /v2/acts/{id}/builds/default`, refreshable with `refresh.py`.

FIXTURES = Path(__file__).parent / "fixtures" / "apify"

#: Every `x.get("field")` in a mapper's source, so a renamed or mistyped field
#: is a test failure rather than a silently empty column.
_GET_RE = re.compile(r'(\w+)\.get\("([A-Za-z_][A-Za-z0-9_]*)"\)')

#: Which local name holds which part of an item. Declared rather than inferred:
#: `loc` and `company_obj` are nested objects, and treating their keys as
#: top-level fields would assert the wrong thing.
_SCOPES = {
    "indeed": {"item": (), "loc": ("location",), "company_obj": ("company",)},
    "linkedin": {"item": ()},
}


def _schema_properties(actor: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / f"{actor}-dataset-schema.json").read_text())
    return data["dataset"]["fields"]["properties"]


def _fields_read(module: Any) -> set[tuple[tuple[str, ...], str]]:
    """(path, field) pairs the mapper reads, extracted from its source."""
    source = Path(module.__file__).read_text()
    scopes = _SCOPES[module.SOURCE]
    return {
        (scopes[holder], name)
        for holder, name in _GET_RE.findall(source)
        if holder in scopes
    }


@pytest.mark.parametrize("module", [indeed, linkedin])
def test_every_field_a_mapper_reads_exists_in_the_published_schema(module) -> None:
    properties = _schema_properties(module.SOURCE)
    missing = []
    for path, name in sorted(_fields_read(module)):
        node = properties
        for parent in path:
            node = node.get(parent, {}).get("properties", {})
        if name not in node:
            missing.append(".".join((*path, name)))
    assert not missing, (
        f"{module.SOURCE} mapper reads fields the actor does not publish: {missing}"
    )


@pytest.mark.parametrize("module", [indeed, linkedin])
def test_a_mapper_survives_every_field_the_actor_can_send(module) -> None:
    """Exercised against the FULL declared shape, not the subset I knew about.

    The item is built from the schema, so a field added by a future build
    arrives here automatically — including the ones no mapper reads, which is
    where a crash would otherwise be found in production.
    """
    item = {**_item_from_schema(module.SOURCE), **_IDENTITY[module.SOURCE]}
    job = module.to_job(item)
    assert job is not None
    assert job.source == module.SOURCE
    assert job.title and job.company and job.job_url


def _item_from_schema(actor: str) -> dict[str, Any]:
    """A dataset item with every declared field populated by its declared type."""
    def value(spec: dict[str, Any], name: str) -> Any:
        declared = spec.get("type")
        kinds = declared if isinstance(declared, list) else [declared]
        kind = next((k for k in kinds if k != "null"), "string")
        if kind == "object":
            return {k: value(v, k) for k, v in (spec.get("properties") or {}).items()}
        if kind == "array":
            items = spec.get("items") or {}
            return [value(items, name)] if items.get("type") == "object" else [name]
        if kind == "number":
            return 1
        if kind == "boolean":
            return True
        return name

    return {k: value(v, k) for k, v in _schema_properties(actor).items()}


#: The few fields that must carry a *usable* value rather than their own name,
#: because the mapper checks identity and would otherwise drop the item.
_IDENTITY = {
    "indeed": {
        "key": "abc123",
        "title": "Senior Backend Engineer",
        "jobUrl": "https://www.indeed.com/viewjob?jk=abc123",
        "company": {"companyName": "Acme Corp", "industrySector": "Software"},
    },
    "linkedin": {
        "jobId": "3912847",
        "jobTitle": "Staff Platform Engineer",
        "companyName": "Globex Industries",
        "jobUrl": "https://www.linkedin.com/jobs/view/3912847",
    },
}


# --- the webhook receiver ---------------------------------------------------


@pytest.fixture()
def hook(monkeypatch, _isolated_db):
    """The receiver with a known secret, and ingestion stubbed out.

    Ingestion is replaced so these tests stay about the HTTP contract: what is
    accepted, what is refused, and whether the response comes back before the
    work does.
    """
    from jobscrape import webhook

    monkeypatch.setattr(config, "APIFY_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(webhook.config, "APIFY_WEBHOOK_SECRET", "s3cret")
    ingested: list[str] = []

    async def fake_ingest(run_id: str) -> None:
        ingested.append(run_id)

    monkeypatch.setattr(webhook, "_ingest", fake_ingest)
    with TestClient(webhook.app) as client:
        yield client, ingested


PAYLOAD = {
    "eventType": "ACTOR.RUN.SUCCEEDED",
    "eventData": {"actorId": "abc", "actorRunId": "RUN123"},
    "resource": {"id": "RUN123", "status": "SUCCEEDED",
                 "defaultDatasetId": "DATASET123"},
}


def test_a_valid_delivery_is_accepted_and_ingested(hook) -> None:
    client, ingested = hook
    response = client.post("/apify", json=PAYLOAD, headers={"X-Apify-Secret": "s3cret"})
    assert response.status_code == 200
    assert response.json()["run_id"] == "RUN123"
    assert ingested == ["RUN123"]


def test_the_secret_may_arrive_in_the_url(hook) -> None:
    """Apify's own docs recommend a URL token, and it is the only option when
    a headers template is awkward to configure."""
    client, ingested = hook
    assert client.post("/apify/s3cret", json=PAYLOAD).status_code == 200
    assert ingested == ["RUN123"]


@pytest.mark.parametrize(
    "headers,path",
    [
        ({}, "/apify"),                                  # none at all
        ({"X-Apify-Secret": "wrong"}, "/apify"),         # wrong header
        ({}, "/apify/wrong"),                            # wrong URL token
    ],
)
def test_a_bad_secret_is_refused(hook, headers: dict, path: str) -> None:
    client, ingested = hook
    response = client.post(path, json=PAYLOAD, headers=headers)
    assert response.status_code == 401
    assert ingested == []


def test_an_unset_secret_refuses_everything(monkeypatch, _isolated_db) -> None:
    """The failure mode of the opposite default is an open ingestion endpoint
    on a route that is meant to be publicly reachable."""
    from jobscrape import webhook

    monkeypatch.setattr(webhook.config, "APIFY_WEBHOOK_SECRET", "")
    with TestClient(webhook.app) as client:
        assert client.post("/apify", json=PAYLOAD).status_code == 401
        assert client.post("/apify/anything", json=PAYLOAD).status_code == 401


@pytest.mark.parametrize("event", ["ACTOR.RUN.FAILED", "ACTOR.RUN.TIMED_OUT", ""])
def test_a_non_success_event_is_acknowledged_but_not_ingested(hook, event: str) -> None:
    """200, not an error. Apify retries a non-2xx eleven times over ~32 hours,
    and this is a delivery we understand and are declining to act on."""
    client, ingested = hook
    response = client.post("/apify", json={**PAYLOAD, "eventType": event},
                           headers={"X-Apify-Secret": "s3cret"})
    assert response.status_code == 200
    assert ingested == []


def test_a_payload_with_no_run_id_is_rejected(hook) -> None:
    client, ingested = hook
    response = client.post("/apify", json={"eventType": "ACTOR.RUN.SUCCEEDED"},
                           headers={"X-Apify-Secret": "s3cret"})
    assert response.status_code == 400
    assert ingested == []


def test_the_run_id_is_read_from_either_place(hook) -> None:
    """Apify puts it in `eventData`; the `resource` block carries it too."""
    client, ingested = hook
    client.post("/apify", json={"eventType": "ACTOR.RUN.SUCCEEDED",
                                "resource": {"id": "FROM_RESOURCE"}},
                headers={"X-Apify-Secret": "s3cret"})
    assert ingested == ["FROM_RESOURCE"]


def test_health_says_nothing_about_the_archive(hook) -> None:
    """This endpoint is public, unlike the dashboard's, which reports row
    counts, disk headroom and the age of the last run."""
    client, _ = hook
    body = client.get("/health").json()
    assert body == {"ok": True}


def test_re_delivery_is_safe() -> None:
    """Apify retries, so the same run arrives more than once.

    Nothing guards against it because nothing needs to: `upsert_jobs` is keyed
    on (source, source_job_id) with ON CONFLICT DO UPDATE, so a second pass
    converges on the same rows instead of duplicating them.
    """
    from jobscrape.models import Job

    with db.session() as conn:
        job = Job(source="indeed", source_job_id="dup-1", title="Backend Engineer",
                  company="Acme", job_url="https://x/1",
                  description_text="Python and Kubernetes.")
        first = db.upsert_jobs(conn, [job])
        second = db.upsert_jobs(conn, [job])
        assert first == (1, 0)
        assert second == (0, 1)
        count = conn.execute(
            "SELECT count(*) AS c FROM jobs WHERE source = ? AND source_job_id = ?",
            ("indeed", "dup-1"),
        ).fetchone()["c"]
        assert count == 1


# --- the ingest path itself -------------------------------------------------
#
# These exist because the feature shipped broken and the suite did not notice.
# `ingest` was loaded through a module `__getattr__` that did
# `from . import ingest`, which re-enters `__getattr__` — `from X import Y`
# resolves Y with `getattr(X, "Y")`. Every real ingestion raised RecursionError.
# Both `apify-pull` and the webhook's background task were dead, and the tests
# passed because they stub `_ingest` and never import the module.


def test_the_ingest_module_can_be_imported() -> None:
    """The whole feature is unreachable if this fails, however green the rest is."""
    from jobscrape.sources.apify import ingest

    assert hasattr(ingest, "ingest_run")
    assert hasattr(ingest, "ingest_and_record")


def test_every_entry_point_can_reach_ingest() -> None:
    """The two importers that were broken, exercised the way they import it."""
    from jobscrape.sources.apify import ingest as via_from_import  # noqa: F401
    import jobscrape.sources.apify as package

    assert package.ingest is via_from_import


def _stub_client(monkeypatch, run: dict | None, items: list[dict]) -> None:
    from jobscrape.sources.apify import client

    async def run_info(_fetcher, _run_id):
        return run

    async def dataset_items(_fetcher, _dataset_id, max_items=None):
        return items[:max_items] if max_items else items

    async def actor_name(_fetcher, actor):
        """The id->name lookup, offline. Returns the id unchanged, so an actor
        named in `actId` resolves and an opaque one does not."""
        return actor

    monkeypatch.setattr(client, "run_info", run_info)
    monkeypatch.setattr(client, "dataset_items", dataset_items)
    monkeypatch.setattr(client, "actor_name", actor_name)
    from jobscrape.sources import apify
    apify._RESOLVED.clear()


SUCCEEDED_RUN = {
    "status": "SUCCEEDED",
    "actId": "cheap_scraper~indeed-job-scraper",
    "defaultDatasetId": "DS1",
}


def test_a_run_is_fetched_mapped_filtered_and_stored(monkeypatch, _isolated_db) -> None:
    from jobscrape.sources.apify import ingest

    _stub_client(monkeypatch, SUCCEEDED_RUN, [INDEED_ITEM])
    result = asyncio.run(ingest.ingest_run(None, _isolated_db, "RUN1"))

    assert result.error is None
    assert (result.items, result.mapped, result.kept) == (1, 1, 1)
    assert result.inserted == 1
    assert result.ats_linked == 1        # the Greenhouse apply URL was read
    assert result.funnel["seen"] == 1

    stored = _isolated_db.execute(
        "SELECT company_slug, city, salary_min FROM jobs WHERE source_job_id = ?",
        ("a1b2c3",),
    ).fetchone()
    assert stored["company_slug"] == "acmecorp"
    assert stored["city"] == "Austin"


def test_since_until_filters_by_posted_date(monkeypatch, _isolated_db) -> None:
    """`ingest_run` accepts the same `since`/`until` date window every other
    source's fetch does — INDEED_ITEM's `datePublished` is 2026-07-30."""
    from datetime import date

    from jobscrape.sources.apify import ingest

    _stub_client(monkeypatch, SUCCEEDED_RUN, [INDEED_ITEM])
    outside = asyncio.run(ingest.ingest_run(
        None, _isolated_db, "RUN1",
        since=date(2026, 8, 1), until=date(2026, 8, 31),
    ))
    assert outside.kept == 0
    assert outside.funnel["dropped_window"] == 1

    _stub_client(monkeypatch, SUCCEEDED_RUN, [INDEED_ITEM])
    inside = asyncio.run(ingest.ingest_run(
        None, _isolated_db, "RUN1",
        since=date(2026, 7, 1), until=date(2026, 7, 31),
    ))
    assert inside.kept == 1


@pytest.mark.parametrize(
    "run,expected",
    [
        (None, "could not be read"),
        ({"status": "FAILED"}, "not SUCCEEDED"),
        ({"status": "SUCCEEDED", "actId": "x", "defaultDatasetId": ""},
         "no defaultDatasetId"),
        ({"status": "SUCCEEDED", "actId": "someone~glassdoor-scraper",
          "defaultDatasetId": "D"}, "no mapper"),
    ],
)
def test_a_run_that_cannot_be_ingested_reports_why(
    monkeypatch, _isolated_db, run: dict | None, expected: str
) -> None:
    """Each failure names itself rather than looking like an empty dataset."""
    from jobscrape.sources.apify import ingest

    _stub_client(monkeypatch, run, [INDEED_ITEM])
    result = asyncio.run(ingest.ingest_run(None, _isolated_db, "RUN1"))
    assert result.error is not None and expected in result.error
    assert result.inserted == 0


def test_the_dataset_id_comes_from_the_run_not_the_caller(
    monkeypatch, _isolated_db
) -> None:
    """The webhook payload is unauthenticated and names both a run and a dataset.

    Apify offers no request signing, so a forged body could otherwise point
    ingestion at any dataset on the account. `ingest_run` takes only a run id
    and re-reads the dataset from the API.
    """
    import inspect

    from jobscrape.sources.apify import ingest

    signature = inspect.signature(ingest.ingest_run)
    assert "dataset_id" not in signature.parameters, (
        "ingest_run must not accept a dataset id from its caller"
    )


def test_ingest_and_record_writes_a_run_row(monkeypatch, _isolated_db) -> None:
    """Ingestion appears on the dashboard's Runs page like every other stage.

    Otherwise this would be the one way postings enter the archive with no
    trace of how they got there.
    """
    from jobscrape.sources.apify import ingest

    _stub_client(monkeypatch, SUCCEEDED_RUN, [INDEED_ITEM])
    asyncio.run(ingest.ingest_and_record(_isolated_db, "RUN1", command="apify-pull"))

    run = _isolated_db.execute(
        "SELECT command, stage, status FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert run["command"] == "apify-pull"
    assert run["stage"] == "apify"
    assert run["status"] == "ok"


# --- `cmd_apify_pull` as a schedule step: it must never abort the schedule --
#
# This is the one command in the daily cron leg that talks to an external API
# rather than a board it already knows the shape of. `scripts/schedule.sh` runs
# under `set -euo pipefail`, so any non-zero exit here — for a state as mundane
# as "no token yet" or "the actors haven't run yet" — would abort the whole
# night's schedule before dedupe and stats ever ran. Both used to return 1.


def _pull_args(**overrides: Any) -> Any:
    import argparse

    defaults = dict(
        run=None, actor=None, last=3, max_results=None, db=None,
        all_roles=False, worldwide=False, all_workplaces=False,
        include_roles=None, include_qa=False, include_management=False,
        since=None, until=None,
    )
    return argparse.Namespace(**{**defaults, **overrides})


def test_a_missing_token_skips_rather_than_fails(monkeypatch, _isolated_db) -> None:
    from jobscrape import cli, config

    monkeypatch.setattr(config, "APIFY_TOKEN", "")
    assert asyncio.run(cli.cmd_apify_pull(_pull_args())) == 0
    assert _isolated_db.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 0


def test_no_runs_yet_skips_rather_than_fails(monkeypatch, _isolated_db) -> None:
    """The state this project is in today: a token, but the actors have never
    completed a run. Only the auto-discover path (no `--run`) may skip —
    see the next test for why an explicit `--run` must still fail."""
    from jobscrape import cli, config
    from jobscrape.sources.apify import client

    monkeypatch.setattr(config, "APIFY_TOKEN", "fake-token-for-this-test")

    async def no_runs(_fetcher, _actor, limit=5):
        return []

    monkeypatch.setattr(client, "last_runs", no_runs)
    assert asyncio.run(cli.cmd_apify_pull(_pull_args())) == 0
    assert _isolated_db.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 0


def test_an_explicit_unresolved_run_id_still_fails(monkeypatch, _isolated_db) -> None:
    """A `--run <id>` that resolves to nothing is a real error — a typo, or a
    run that was cleaned up — and must not be swallowed the way auto-discovery
    finding nothing is."""
    from jobscrape import cli, config

    monkeypatch.setattr(config, "APIFY_TOKEN", "fake-token-for-this-test")
    _stub_client(monkeypatch, None, [])  # run_info returns None -> "not found"

    result = asyncio.run(cli.cmd_apify_pull(_pull_args(run=["RUN-DOES-NOT-EXIST"])))
    assert result == 1


def test_blank_actor_config_contributes_no_request(monkeypatch, _isolated_db) -> None:
    """`APIFY_INDEED_ACTOR=` (cleared in .env) must not become a request to
    `/v2/acts//runs` — empty strings are filtered before the actor list is
    built, not passed through to the HTTP layer."""
    from jobscrape import cli, config
    from jobscrape.sources.apify import client

    monkeypatch.setattr(config, "APIFY_TOKEN", "fake-token-for-this-test")
    monkeypatch.setattr(config, "APIFY_INDEED_ACTOR", "")
    monkeypatch.setattr(config, "APIFY_LINKEDIN_ACTOR", "cheap_scraper~linkedin-job-scraper")

    seen: list[str] = []

    async def record_actor(_fetcher, actor, limit=5):
        seen.append(actor)
        return []

    monkeypatch.setattr(client, "last_runs", record_actor)
    asyncio.run(cli.cmd_apify_pull(_pull_args()))
    assert seen == ["cheap_scraper~linkedin-job-scraper"]


def test_apify_pull_forwards_the_role_and_workplace_flags(monkeypatch, _isolated_db) -> None:
    """Apify postings must obey the same rules as every other source — a
    `--all-roles`/`--worldwide`/`--all-workplaces` on the CLI has to reach
    `apply_filters_counted` through `ingest_run`, not be silently dropped
    between `cmd_apify_pull` and the filter."""
    from jobscrape import cli

    captured: dict[str, Any] = {}

    async def fake_ingest_and_record(conn, run_id, *, command, **kw):
        captured.update(kw)

        class _Result:
            error = None
            funnel = None

        return _Result()

    monkeypatch.setattr(
        "jobscrape.sources.apify.ingest.ingest_and_record", fake_ingest_and_record
    )
    from jobscrape import config as cfg
    monkeypatch.setattr(cfg, "APIFY_TOKEN", "fake-token-for-this-test")

    asyncio.run(cli.cmd_apify_pull(_pull_args(
        run=["RUN1"], all_roles=True, worldwide=True, all_workplaces=True,
    )))

    assert captured["software_only"] is False
    assert captured["us_only"] is False
    assert captured["remote_only"] is False
