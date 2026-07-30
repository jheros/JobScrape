"""Job source adapters.

Two families:

* :class:`BoardSource` — per-company ATS boards, needing a discovered slug.
  These are the coverage backbone.
* :class:`GlobalSource` — search-style APIs queried once for the whole market.
"""

from __future__ import annotations

from .base import BoardSource, GlobalSource, Source
from . import (
    adp,
    arbeitnow,
    ashby,
    adzuna,
    bamboohr,
    breezy,
    cornerstone,
    greenhouse,
    hackernews,
    himalayas,
    icims,
    jazzhr,
    jobicy,
    jsearch,
    jobvite,
    jooble,
    lever,
    oracle,
    paycom,
    personio,
    pinpoint,
    recruitee,
    remoteok,
    remotive,
    rippling,
    smartrecruiters,
    teamtailor,
    ukg,
    usajobs,
    weworkremotely,
    workable,
    workday,
)

BOARD_SOURCES: dict[str, BoardSource] = {
    s.ats: s
    for s in (
        # Single-request boards: cheapest, so they run first.
        greenhouse.Greenhouse(),
        lever.Lever(),
        recruitee.Recruitee(),
        pinpoint.Pinpoint(),
        teamtailor.Teamtailor(),
        personio.Personio(),
        # Two-request boards: a detail call per posting.
        ashby.Ashby(),
        smartrecruiters.SmartRecruiters(),
        workable.Workable(),
        bamboohr.BambooHR(),
        breezy.Breezy(),
        workday.Workday(),
        rippling.Rippling(),
        # Enterprise ATSes: large US employers, added after freehire's review.
        oracle.Oracle(),
        ukg.UKG(),
        adp.ADP(),
        # Bootstrap-then-search boards: one page fetch for a session token,
        # then a normal JSON API. Added after the ats-scrapers review corrected
        # an earlier call that both of these were auth-gated and unreachable.
        cornerstone.Cornerstone(),
        paycom.Paycom(),
        # HTML/sitemap boards whose postings carry schema.org data, so they are
        # as reliable as the JSON sources.
        jazzhr.JazzHR(),
        jobvite.Jobvite(),
        icims.ICIMS(),
    )
}

GLOBAL_SOURCES: dict[str, GlobalSource] = {
    s.name: s
    for s in (
        # Keyless.
        remoteok.RemoteOK(),
        arbeitnow.Arbeitnow(),
        hackernews.HackerNews(),
        himalayas.Himalayas(),
        remotive.Remotive(),
        jobicy.Jobicy(),
        weworkremotely.WeWorkRemotely(),
        # Need a free API key; skipped with a message when absent.
        usajobs.USAJobs(),
        adzuna.Adzuna(),
        jooble.Jooble(),
        jsearch.JSearch(),
    )
}


def restricted_sources() -> dict[str, GlobalSource]:
    """Opt-in search-style adapters for sites whose ToS forbid scraping.

    Imported lazily and only when explicitly requested, so a breakage here can
    never affect a normal run.
    """
    from .restricted import indeed, linkedin

    return {s.name: s for s in (linkedin.LinkedIn(), indeed.Indeed())}


def restricted_board_sources() -> dict[str, BoardSource]:
    """Opt-in per-company boards on enterprise portals.

    Separate from :func:`restricted_sources` because these are addressed by a
    discovered slug rather than a search query, so the fetch loop drives them
    like any other board.
    """
    from .restricted import avature

    return {s.ats: s for s in (avature.Avature(),)}


ALL_SOURCE_NAMES = sorted(set(BOARD_SOURCES) | set(GLOBAL_SOURCES))

__all__ = [
    "Source", "BoardSource", "GlobalSource",
    "BOARD_SOURCES", "GLOBAL_SOURCES", "ALL_SOURCE_NAMES",
    "restricted_sources", "restricted_board_sources",
]
