"""Tests for the discovery vectors added to widen the company funnel.

Both exist because Common Crawl is neither complete nor always available — it
began refusing connections entirely mid-project — so each must work on its own.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobscrape.discovery import certtransparency as ct
from jobscrape.discovery import github_lists as gh


class FakeFetcher:
    def __init__(self, json_payload: Any = None, text: Any = None) -> None:
        self._json = json_payload
        self._text = text

    async def get_json(self, url: str, **kw: Any) -> Any:
        return self._json

    async def get_text(self, url: str, **kw: Any) -> Any:
        return self._text


# --- certificate transparency ---------------------------------------------

def test_ct_reduces_hostnames_to_customer_labels() -> None:
    names = [
        "acme.teamtailor.com",
        "*.wildcard.teamtailor.com",
        "careers.eu.multi.teamtailor.com",   # multi-label: not a board slug
        "www.teamtailor.com",               # vendor's own site
        "assets.teamtailor.com",            # vendor asset host
        "somethingelse.example.com",        # different domain
        "globex.teamtailor.com",
    ]
    assert ct._subdomains(names, "teamtailor.com") == {"acme", "globex", "wildcard"}


def test_ct_drops_vendor_subdomains() -> None:
    """Probing the vendor's own hosts wastes a request on every run."""
    names = [f"{s}.bamboohr.com" for s in ("www", "app", "api", "blog", "acme")]
    assert ct._subdomains(names, "bamboohr.com") == {"acme"}


def test_ct_drops_vendor_infrastructure_labels() -> None:
    """The junk that was actually in the registry.

    Twenty iCIMS *infrastructure* hosts had been recorded as boards —
    `analytics`, `billing`, `autodiscover` and eleven `api-<region>` variants.
    Each is re-probed on every validate pass and can never be anything but
    dead, so it is junk that costs request budget rather than junk that merely
    looks untidy. The `api-` family is checked by prefix because its suffix is
    unbounded: one per region, per stage, per version.
    """
    labels = ("analytics", "billing", "autodiscover", "gslink", "agents",
              "api-eu-west-1", "api-stg", "analytics-api", "acme")
    names = [f"{s}.icims.com" for s in labels]
    assert ct._subdomains(names, "icims.com") == {"acme"}


async def test_ct_certspotter_parsing() -> None:
    payload = [
        {"dns_names": ["acme.teamtailor.com", "www.teamtailor.com"]},
        {"dns_names": ["globex.teamtailor.com"]},
    ]
    found = await ct.from_certspotter(FakeFetcher(json_payload=payload), "teamtailor.com")
    assert found == {"acme", "globex"}


async def test_ct_hackertarget_parsing() -> None:
    csv = "teamtailor.com,54.1.1.1\nacme.teamtailor.com,54.1.1.2\nglobex.teamtailor.com,54.1.1.3\n"
    found = await ct.from_hackertarget(FakeFetcher(text=csv), "teamtailor.com")
    assert found == {"acme", "globex"}


async def test_ct_hackertarget_quota_message_is_not_data() -> None:
    quota = "error API count exceeded - Increase Quota with Membership"
    assert await ct.from_hackertarget(FakeFetcher(text=quota), "teamtailor.com") == set()


async def test_ct_crtsh_handles_non_json() -> None:
    """crt.sh frequently answers 404/502 with an HTML body."""
    assert await ct.from_crtsh(FakeFetcher(text="<html>502</html>"), "teamtailor.com") == set()
    assert await ct.from_crtsh(FakeFetcher(text=None), "teamtailor.com") == set()


async def test_ct_one_failing_provider_does_not_stop_the_others(monkeypatch) -> None:
    async def boom(fetcher: Any, domain: str) -> set[str]:
        raise RuntimeError("provider down")

    async def ok(fetcher: Any, domain: str) -> set[str]:
        return {"acme"}

    monkeypatch.setattr(ct, "from_certspotter", boom)
    monkeypatch.setattr(ct, "from_hackertarget", ok)
    monkeypatch.setattr(ct, "from_crtsh", boom)
    assert await ct.discover_domain(FakeFetcher(), "teamtailor.com") == {"acme"}


def test_ct_targets_cover_the_subdomain_platforms() -> None:
    """Only subdomain-per-customer platforms can be enumerated this way."""
    ats_names = set(ct.CT_TARGETS.values())
    for expected in ("teamtailor", "bamboohr", "breezy", "recruitee", "pinpoint", "jazzhr"):
        assert expected in ats_names


# --- curated GitHub lists -------------------------------------------------

MARKDOWN = """
| Company | Role | Link |
| Stripe | SWE | https://job-boards.greenhouse.io/stripe/jobs/123 |
| Spotify | Backend | https://jobs.lever.co/spotify/abc-def |
| Ramp | Platform | https://jobs.ashbyhq.com/ramp/uuid-1 |
| Acme | Eng | https://acme.bamboohr.com/careers/44 |
| Globex | Eng | https://globex.breezy.hr/p/xyz |
| NVIDIA | SWE | https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x_JR1 |
| Vendor | n/a | https://www.bamboohr.com/pricing |
"""


def test_github_extract_covers_path_and_subdomain_boards() -> None:
    found = gh.extract(MARKDOWN)
    assert found["greenhouse"] == {"stripe"}
    assert found["lever"] == {"spotify"}
    assert found["ashby"] == {"ramp"}
    assert found["bamboohr"] == {"acme"}     # vendor's www host excluded
    assert found["breezy"] == {"globex"}


def test_github_extract_builds_workday_triples() -> None:
    """Workday needs tenant|wd|site, and the locale segment is not the site."""
    found = gh.extract(MARKDOWN)
    assert found["workday"] == {"nvidia|wd5|NVIDIAExternalCareerSite"}


def test_github_extract_on_empty_input() -> None:
    assert gh.extract("") == {}
    assert gh.extract("no links here at all") == {}


async def test_github_unavailable_repo_is_skipped() -> None:
    found = await gh.discover_all(FakeFetcher(text=None), repos=[("owner/repo", ("main",))])
    assert found == {}


async def test_github_discover_merges_repos() -> None:
    found = await gh.discover_all(
        FakeFetcher(text=MARKDOWN), repos=[("a/b", ("main",)), ("c/d", ("main",))]
    )
    assert found["greenhouse"] == {"stripe"}
