"""hiring.cafe mapper/scraper/ingest.

Field names in the fixtures below match a real live response captured while
building this adapter (see `scraper.py`'s module docstring for how) --
notably `job_information.title` (not a description field, which does not
exist on the list endpoint at all) and `v5_processed_job_data`'s already-
annualized `yearly_min_compensation`/`yearly_max_compensation`.
"""

from __future__ import annotations

from typing import Any

from jobscrape import db
from jobscrape.sources.hiringcafe import ingest as ingest_module
from jobscrape.sources.hiringcafe import mapper, scraper


def _record(**overrides: dict[str, Any]) -> dict[str, Any]:
    v5 = {
        "company_name": "Acme",
        "core_job_title": "Software Engineer",
        "commitment": ["Full Time"],
        "job_category": "Software Engineering",
        "company_sector_and_industry": "Information Technology",
        "formatted_workplace_location": "Remote, United States",
        "workplace_type": "Remote",
        "seniority_level": "Senior Level",
        "listed_compensation_currency": "USD",
        "yearly_min_compensation": 140000,
        "yearly_max_compensation": 170000,
        "estimated_publish_date": "2026-07-10T17:34:05.687Z",
        "role_activities": ["developing features", "mentoring engineers"],
        "requirements_summary": "5+ years of backend experience",
        "technical_tools": ["Python", "Postgres"],
        "company_tagline": "We build developer tools.",
        **overrides.pop("v5", {}),
    }
    job_information = {"title": "Senior Software Engineer", **overrides.pop("job_information", {})}
    record = {
        "id": "acme___1___abc123",
        "objectID": "acme___1___abc123",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/4021",
        "is_expired": False,
        "source": "greenhouse",
        "v5_processed_job_data": v5,
        "job_information": job_information,
        **overrides,
    }
    return record


# --- mapper -----------------------------------------------------------------


def test_mapper_happy_path() -> None:
    job = mapper.to_job(_record())
    assert job is not None
    assert job.source == "hiringcafe"
    assert job.source_job_id == "acme___1___abc123"
    assert job.title == "Senior Software Engineer"
    assert job.company == "Acme"
    assert job.apply_url == "https://boards.greenhouse.io/acme/jobs/4021"
    assert job.job_url == job.apply_url


def test_expired_records_are_skipped() -> None:
    assert mapper.to_job(_record(is_expired=True)) is None


def test_missing_apply_url_is_skipped() -> None:
    assert mapper.to_job(_record(apply_url=None)) is None


def test_missing_title_is_skipped() -> None:
    assert mapper.to_job(_record(job_information={"title": ""}, v5={"core_job_title": ""})) is None


def test_missing_company_is_skipped() -> None:
    assert mapper.to_job(_record(v5={"company_name": ""})) is None


def test_title_falls_back_to_core_job_title_when_the_real_title_is_blank() -> None:
    job = mapper.to_job(_record(job_information={"title": ""}))
    assert job is not None
    assert job.title == "Software Engineer"


def test_seniority_level_mapping_table() -> None:
    cases = {
        "No Prior Experience Required": "intern",
        "Entry Level": "entry",
        "Mid Level": "mid",
        "Senior Level": "senior",
    }
    for raw, expected in cases.items():
        job = mapper.to_job(_record(v5={"seniority_level": raw}))
        assert job is not None
        assert job.seniority == expected, raw


def test_an_unrecognized_seniority_level_falls_through_to_title_derivation() -> None:
    """`Job.__post_init__` only fills a blank `seniority` from the title --
    an unrecognized `seniority_level` must leave the mapper's own value
    blank so that still happens, matching the contract
    `test_seniority.py::test_an_adapter_supplied_level_is_not_overwritten`
    documents for every other adapter."""
    job = mapper.to_job(_record(
        v5={"seniority_level": "Some New Category", "core_job_title": "Staff Engineer"},
        job_information={"title": "Staff Engineer"},
    ))
    assert job is not None
    assert job.seniority == "staff"


def test_salary_passes_through_without_annualising() -> None:
    """Regression guard: hiring.cafe's yearly_min/max_compensation is
    already annualized, unlike jsearch.py/JobSpy -- this must never grow an
    `annualise()` call."""
    job = mapper.to_job(_record(v5={"yearly_min_compensation": 100000,
                                     "yearly_max_compensation": 120000}))
    assert job is not None
    assert job.salary_min == 100000
    assert job.salary_max == 120000


def test_description_is_composed_even_though_the_source_has_no_description_field() -> None:
    record = _record()
    assert "description" not in record["job_information"]
    job = mapper.to_job(record)
    assert job is not None
    assert job.description_text
    assert "developing features" in job.description_text


def test_company_slug_resolves_via_a_known_ats_board() -> None:
    job = mapper.to_job(_record(apply_url="https://boards.greenhouse.io/acme/jobs/4021"))
    assert job is not None
    assert job.company_slug == "acme"


def test_company_slug_falls_back_to_a_slugified_company_name_off_an_unknown_board() -> None:
    job = mapper.to_job(_record(
        apply_url="https://acme.com/careers/123",
        v5={"company_name": "Bright Vision Technologies"},
    ))
    assert job is not None
    assert job.company_slug == "bright-vision-technologies"


def test_to_jobs_drops_unusable_records_and_keeps_the_rest() -> None:
    jobs = mapper.to_jobs([_record(), _record(is_expired=True), {"not": "a job"}])
    assert len(jobs) == 1


# --- scraper ------------------------------------------------------------


class FakeFetcher:
    """Queues of canned responses, consumed in call order."""

    def __init__(self, homepage_htmls: list[str | None], json_responses: list[Any]) -> None:
        self._homepage_htmls = list(homepage_htmls)
        self._json_responses = list(json_responses)
        self.get_text_calls = 0
        self.get_json_urls: list[str] = []

    async def get_text(self, url: str, **kw: Any) -> str | None:
        self.get_text_calls += 1
        return self._homepage_htmls.pop(0) if self._homepage_htmls else None

    async def get_json(self, url: str, **kw: Any) -> Any:
        self.get_json_urls.append(url)
        return self._json_responses.pop(0) if self._json_responses else None


def _homepage(build_id: str) -> str:
    return f'<html><script id="__NEXT_DATA__">{{"buildId":"{build_id}","props":{{}}}}</script></html>'


def _page(hits: list[dict], is_last: bool) -> dict:
    return {"pageProps": {"ssrHits": hits, "ssrIsLastPage": is_last}, "__N_SSP": True}


async def test_get_build_id_extracts_the_token_from_next_data() -> None:
    fetcher = FakeFetcher([_homepage("abc123")], [])
    assert await scraper.get_build_id(fetcher) == "abc123"


async def test_get_build_id_returns_none_when_the_homepage_fetch_fails() -> None:
    fetcher = FakeFetcher([None], [])
    assert await scraper.get_build_id(fetcher) is None


async def test_fetch_page_unwraps_pageprops() -> None:
    fetcher = FakeFetcher([], [_page([{"id": "1"}], True)])
    props = await scraper.fetch_page(fetcher, "bid", {"searchQuery": "x"}, 0)
    assert props == {"ssrHits": [{"id": "1"}], "ssrIsLastPage": True}


async def test_fetch_page_returns_none_when_pageprops_is_missing() -> None:
    fetcher = FakeFetcher([], [{"notPageProps": 1}])
    assert await scraper.fetch_page(fetcher, "bid", {}, 0) is None


async def test_collect_pages_until_ssr_is_last_page() -> None:
    fetcher = FakeFetcher(
        [_homepage("bid1")],
        [_page([{"id": "1"}], False), _page([{"id": "2"}], True)],
    )
    hits, note = await scraper.collect(fetcher, "software engineer", max_pages=10)
    assert [h["id"] for h in hits] == ["1", "2"]
    assert note == ""


async def test_collect_stops_at_max_pages_even_when_more_are_available() -> None:
    fetcher = FakeFetcher(
        [_homepage("bid1")],
        [_page([{"id": "1"}], False), _page([{"id": "2"}], False)],
    )
    hits, note = await scraper.collect(fetcher, "software engineer", max_pages=2)
    assert len(hits) == 2


async def test_collect_stops_on_a_page_with_zero_hits() -> None:
    fetcher = FakeFetcher([_homepage("bid1")], [_page([], False)])
    hits, note = await scraper.collect(fetcher, "software engineer", max_pages=10)
    assert hits == []
    assert note == ""


async def test_collect_refreshes_the_build_id_once_on_a_failed_page_then_retries() -> None:
    """A stale buildId 404s cleanly (confirmed live -- see scraper.py) and
    `fetch_page` surfaces that as `None`; `collect()` must refresh the
    buildId exactly once and retry the same page under it."""
    fetcher = FakeFetcher(
        [_homepage("stale"), _homepage("fresh")],
        [None, _page([{"id": "1"}], True)],
    )
    hits, note = await scraper.collect(fetcher, "software engineer", max_pages=10)
    assert [h["id"] for h in hits] == ["1"]
    assert fetcher.get_text_calls == 2
    assert note == ""


async def test_collect_gives_up_after_one_failed_refresh_not_an_infinite_loop() -> None:
    fetcher = FakeFetcher(
        [_homepage("stale"), _homepage("still-broken")],
        [None, None],
    )
    hits, note = await scraper.collect(fetcher, "software engineer", max_pages=10)
    assert hits == []
    assert fetcher.get_text_calls == 2, "must refresh exactly once, not loop"
    assert "twice" in note


async def test_collect_reuses_a_caller_supplied_build_id_instead_of_fetching_one() -> None:
    """`cmd_hiringcafe_fetch` fetches the buildId once and reuses it across
    every search term -- see scraper.py's `collect()` docstring."""
    fetcher = FakeFetcher([], [_page([{"id": "1"}], True)])
    hits, note = await scraper.collect(
        fetcher, "software engineer", max_pages=10, build_id="reused-bid")
    assert [h["id"] for h in hits] == ["1"]
    assert fetcher.get_text_calls == 0


# --- ingest -------------------------------------------------------------


def test_ingest_and_record_inherits_an_existing_run_id(monkeypatch) -> None:
    with db.session() as conn:
        run_id = db.start_run(conn, command="test", stage="hiringcafe")
    monkeypatch.setenv("JOBSCRAPE_RUN_ID", str(run_id))
    with db.session() as conn:
        ingest_module.ingest_and_record(
            conn, [_record()], software_only=False, us_only=False, remote_only=False,
        )
        # `jobscrape_test` is a persistent database (see conftest.py), so a
        # bare COUNT(*) could pick up unrelated rows from other runs of this
        # suite -- scoping to `id > run_id` looks only at what THIS test
        # could have created.
        newer = conn.execute(
            "SELECT COUNT(*) AS c FROM runs WHERE stage = ? AND id > ?",
            ("hiringcafe", run_id),
        ).fetchall()[0]["c"]
        status = conn.execute(
            "SELECT status FROM runs WHERE id = ?", (run_id,)
        ).fetchall()[0]["status"]
    assert newer == 0, "must reuse the inherited run row, not open a second one"
    assert status == "ok"


def test_ingest_and_record_rolls_back_and_records_failure_on_a_db_error(monkeypatch) -> None:
    def boom(conn: object, jobs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "upsert_jobs", boom)
    with db.session() as conn:
        result = ingest_module.ingest_and_record(
            conn, [_record()], software_only=False, us_only=False, remote_only=False,
        )
    assert result.error == "boom"
