"""Tenant-list import: slug normalisation.

The published lists carry whatever identifier each upstream scraper used, and
those are not our slugs. Getting this wrong is quiet: a mis-normalised slug
imports fine, probes as dead, and leaves a company row that never yields a job
— so the board looks absent rather than mis-addressed.

Every case below is a real row from ats-scrapers' CSVs.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from jobscrape.discovery.tenant_lists import (
    PLATFORM_ATS, iter_lists, normalise, read_list,
)


# --- the six platforms that need real parsing ------------------------------


def test_workday_takes_the_datacentre_from_the_url() -> None:
    """`wd5` appears ONLY in the URL. The slug column reads "23andme/23", so a
    slug-column import would default the datacentre and 404 for every tenant
    not on wd1."""
    assert normalise(
        "workday", "23andme/23", "https://23andme.wd5.myworkdayjobs.com/23"
    ) == "23andme|wd5|23"
    assert normalise(
        "workday", "2020companies/external_careers",
        "https://2020companies.wd1.myworkdayjobs.com/external_careers",
    ) == "2020companies|wd1|external_careers"
    # A locale segment sits between host and site on some tenants.
    assert normalise(
        "workday", "x/y", "https://acme.wd3.myworkdayjobs.com/en-US/careers"
    ) == "acme|wd3|careers"
    # No parseable URL means no guess.
    assert normalise("workday", "acme", "https://acme.example.com/jobs") is None


def test_cornerstone_keeps_the_career_site_id() -> None:
    """A tenant runs several career sites and each lists different jobs, so the
    site id is part of the identity rather than a default."""
    assert normalise(
        "cornerstone", "aswatsoneurope",
        "https://aswatsoneurope.csod.com/ux/ats/careersite/1/home?c=aswatsoneurope",
    ) == "aswatsoneurope|1"
    assert normalise(
        "cornerstone",
        "https://aswatsoneurope.csod.com/ux/ats/careersite/16/home?c=aswatsoneurope",
        "https://aswatsoneurope.csod.com/ux/ats/careersite/16/home?c=aswatsoneurope",
    ) == "aswatsoneurope|16"
    # A bare tenant means Cornerstone's default site.
    assert normalise("cornerstone", "acme", "") == "acme|1"


def test_oracle_accepts_a_vanity_host() -> None:
    """Tenants front the same product on their own domain; pinning the host to
    oraclecloud.com silently dropped those."""
    assert normalise(
        "oracle", "ebwg",
        "https://ebwg.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX",
    ) == "ebwg.fa.us2.oraclecloud.com/CX"
    assert normalise(
        "oracle", "alithya",
        "https://careers.alithya.com/hcmUI/CandidateExperience/en/sites/AlithyaCareersCarrieres",
    ) == "careers.alithya.com/AlithyaCareersCarrieres"


def test_ukg_drops_the_jobboard_path_segment() -> None:
    assert normalise(
        "ukg", "https://recruiting.ultipro.com/AIS1000AISI/JobBoard/b22b728d-47a6-4550-9005-01c83b9a527f", "",
    ) == "recruiting.ultipro.com/AIS1000AISI/b22b728d-47a6-4550-9005-01c83b9a527f"


def test_adp_pairs_the_client_and_company_ids() -> None:
    assert normalise(
        "adp", "7d58836c-11dd-4415-9de0-63b918b88652/19000101_000001",
        "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
        "?cid=7d58836c-11dd-4415-9de0-63b918b88652&ccId=19000101_000001&lang=en_US",
    ) == "7d58836c-11dd-4415-9de0-63b918b88652:19000101_000001"


def test_icims_only_reduces_the_careers_prefix() -> None:
    """iCIMS boards use many subdomain prefixes. `board_host` re-expands a bare
    slug to `careers-<slug>.icims.com`, so reducing an `external-` host would
    produce a hostname that does not exist — it was costing 715 of 2,498."""
    assert normalise("icims", "142designgroup",
                     "https://careers-142designgroup.icims.com") == "142designgroup"
    for host in ("external-92y", "resume-chesterton", "jobs-accion",
                 "drivercareers-adspipe"):
        assert normalise("icims", f"https://{host}.icims.com",
                         f"https://{host}.icims.com") == f"{host}.icims.com"
    # A vanity domain keeps its whole hostname.
    assert normalise("icims", "x", "https://careers.docusign.com") == "careers.docusign.com"


def test_paycom_requires_a_real_portal_token() -> None:
    token = "0013bfed4f70d85bb26d0961bcf5695e"
    assert normalise("paycom", token, "") == token
    assert normalise(
        "paycom", "not-a-token",
        f"https://www.paycomonline.net/v4/ats/web.php/portal/{token}/career-page",
    ) == token
    assert normalise("paycom", "nope", "") is None


def test_ashby_preserves_url_encoded_board_names() -> None:
    """50 boards are named with spaces; the slug column lower-cases and
    un-encodes them, which does not round-trip to a working API path."""
    assert normalise(
        "ashby", "a1 garage door service",
        "https://jobs.ashbyhq.com/A1%20Garage%20Door%20Service",
    ) == "A1%20Garage%20Door%20Service"


def test_jobvite_strips_the_careers_path_prefix() -> None:
    assert normalise("jobvite", "careers/leantechio",
                     "https://jobs.jobvite.com/careers/leantechio") == "leantechio"


# --- pass-through platforms -------------------------------------------------


@pytest.mark.parametrize("platform", ["greenhouse", "lever", "bamboohr", "personio"])
def test_bare_slugs_pass_through(platform: str) -> None:
    assert normalise(platform, "10beauty", "https://x/10beauty") == "10beauty"


@pytest.mark.parametrize("bad", ["", "  ", "has space", "a/b", "http://x"])
def test_a_slug_that_is_not_bare_is_skipped_not_guessed_at(bad: str) -> None:
    """A wrong slug costs a probe and lands as a dead company forever."""
    assert normalise("greenhouse", bad, "") is None


# --- file reading -----------------------------------------------------------


def test_read_list_deduplicates_and_counts_skips(tmp_path: Path) -> None:
    path = tmp_path / "greenhouse.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "slug", "url"])
        writer.writerow(["A", "acme", "https://x/acme"])
        writer.writerow(["A again", "acme", "https://x/acme"])
        writer.writerow(["Bad", "has space", "https://x"])
    slugs, skipped = read_list(path, "greenhouse")
    assert slugs == ["acme"]
    assert skipped == 1


def test_only_platforms_with_an_adapter_are_read(tmp_path: Path) -> None:
    """A company we cannot fetch is a row that fails validation forever."""
    for name in ("greenhouse", "join_com"):
        with (tmp_path / f"{name}.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["name", "slug", "url"])
            writer.writerow(["A", "acme", "https://x/acme"])
    seen = {platform for platform, _, _, _ in iter_lists(tmp_path)}
    assert seen == {"greenhouse"}


def test_every_mapped_platform_has_an_adapter() -> None:
    """Guards against a mapping entry outliving the adapter it names."""
    from jobscrape.sources import BOARD_SOURCES

    assert set(PLATFORM_ATS.values()) <= set(BOARD_SOURCES)


def test_import_leaves_already_validated_boards_alone(_isolated_db) -> None:
    """A re-import must never reset a live board to unchecked."""
    from jobscrape import db
    from jobscrape.discovery import tenant_lists

    conn = _isolated_db
    db.add_companies(conn, "greenhouse", ["acme"])
    db.mark_company(conn, "greenhouse", "acme", "live", 12)

    directory = Path(__file__).parent / "_tmp_tenants"
    directory.mkdir(exist_ok=True)
    try:
        with (directory / "greenhouse.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["name", "slug", "url"])
            writer.writerow(["Acme", "acme", "https://x/acme"])
            writer.writerow(["New", "newco", "https://x/newco"])
        added = tenant_lists.import_directory(conn, directory)
        assert added["greenhouse"] == 1, "only the unseen slug is added"
        row = conn.execute(
            "SELECT status FROM companies WHERE ats=? AND slug=?", ("greenhouse", "acme")
        ).fetchone()
        assert row["status"] == "live"
    finally:
        for leftover in directory.glob("*"):
            leftover.unlink()
        directory.rmdir()
