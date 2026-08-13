"""Search hiring.cafe's own internal Next.js data endpoint.

hiringcafe.com's frontend is a Next.js app; its search results come from a
server-rendered data route, `GET /_next/data/{buildId}/index.json`, which
returns the exact same JSON payload the page itself hydrates from
(`pageProps.ssrHits`, paginated via `ssrIsLastPage`). This was found by
inspecting the site's own network traffic (Playwright), not from any
published API doc -- there isn't one. The site's older, README-documented
API (`POST /api/search-jobs`) is confirmed dead (405).

`{buildId}` is a Next.js deploy-specific token, embedded in the homepage's
`__NEXT_DATA__` script tag. It changes whenever hiring.cafe redeploys their
frontend, so it's fetched fresh at the start of every run rather than
hardcoded, and a stale one is detected and retried: confirmed live that a
bad buildId 404s cleanly (not a wrong-but-200 response), so a
refresh-once-and-retry is safe.

**Location scoping is not optional.** Omitting `searchState.locations`
entirely does not mean "worldwide" -- the backend geolocates the
*requester's own IP* and defaults to that country, confirmed live: a request
sent with no `locations` came back with `initialSearchState.locations ==
[{"formatted_address": "United States", ...}]` purely because the request
happened to originate from a US-ish IP. That's exactly wrong for a source
that might run from any server -- a box hosted outside the US would
silently get non-US results with no error at all. `_US_LOCATION` below is
that same shape, captured live, but with the continental US's geographic
centroid in place of any one requester's IP-derived point (confirmed live
to return the identical total result count as the IP-defaulted request) --
explicit and IP-independent instead of implicit and IP-dependent.

`is_expired` records are real but rare (`False` in every live sample seen)
-- `mapper.py` skips them. The list endpoint carries **no prose
description** at all (`job_information` has no description key in any live
sample) -- `mapper.py` composes a short one from `v5_processed_job_data`'s
structured fields instead; see that module for the composed-not-scraped
disclosure.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ...http import Fetcher

log = logging.getLogger("jobscrape.hiringcafe")

HOMEPAGE = "https://hiringcafe.com/"
DATA_URL = "https://hiringcafe.com/_next/data/{build_id}/index.json"

_BUILD_ID_RE = re.compile(r'"buildId"\s*:\s*"([^"]+)"')

# Captured live from the site's own default (IP-geolocated) `initialSearchState`
# for a US-based request, with the requester-specific lat/lon swapped for the
# continental US's centroid -- see module docstring for why this is passed
# explicitly rather than left out.
_US_LOCATION: dict[str, Any] = {
    "formatted_address": "United States",
    "types": ["country"],
    "geometry": {"location": {"lat": 39.8283, "lon": -98.5795}},
    "id": "user_country",
    "address_components": [
        {"long_name": "United States", "short_name": "US", "types": ["country"]}
    ],
    "options": {"flexible_regions": ["anywhere_in_continent", "anywhere_in_world"]},
}


async def get_build_id(fetcher: Fetcher) -> str | None:
    """The current Next.js buildId, read fresh off the homepage.

    One cheap request, never cached -- a cached, stale HTML page would hand
    back the same stale buildId `collect()`'s retry path is trying to
    escape, defeating the retry entirely.
    """
    html = await fetcher.get_text(HOMEPAGE, cache=False)
    if not html:
        return None
    match = _BUILD_ID_RE.search(html)
    return match.group(1) if match else None


def _search_state(search_term: str) -> dict[str, Any]:
    return {"searchQuery": search_term, "locations": [_US_LOCATION]}


async def fetch_page(
    fetcher: Fetcher, build_id: str, search_state: dict[str, Any], page: int,
) -> dict[str, Any] | None:
    """One page of `pageProps` from the data endpoint, or `None` on failure.

    A `None` here is ambiguous between "buildId went stale (404)" and any
    other request failure -- `Fetcher.request()` swallows the status code on
    every >=400 response alike. `collect()` doesn't need to tell them apart:
    either way, the one thing worth trying is one buildId refresh, once.
    """
    url = DATA_URL.format(build_id=build_id)
    data = await fetcher.get_json(
        url, params={"searchState": json.dumps(search_state), "page": str(page)},
    )
    if not isinstance(data, dict):
        return None
    props = data.get("pageProps")
    return props if isinstance(props, dict) else None


async def collect(
    fetcher: Fetcher, search_term: str, max_pages: int, build_id: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Page one search term to completion.

    Stops at `ssrIsLastPage`, `max_pages`, or a page with zero hits.
    Mirrors `jobright/scraper.py`'s `collect()`: one dead page must not lose
    an otherwise-successful sweep, so a failure here returns whatever was
    already collected plus a short note, rather than raising.

    `build_id` lets a multi-term caller (`cmd_hiringcafe_fetch`) fetch it
    once and reuse it across every search term instead of re-reading the
    homepage per term -- left blank, one is fetched fresh here.
    """
    search_state = _search_state(search_term)
    if not build_id:
        build_id = await get_build_id(fetcher)
    if not build_id:
        return [], "could not read the current buildId off the homepage"

    hits: list[dict[str, Any]] = []
    refreshed_build_id = False
    page = 0
    while page < max_pages:
        props = await fetch_page(fetcher, build_id, search_state, page)
        if props is None:
            if refreshed_build_id:
                return hits, f"page {page} failed twice (after a buildId refresh) -- stopping"
            new_build_id = await get_build_id(fetcher)
            refreshed_build_id = True
            if not new_build_id or new_build_id == build_id:
                return hits, f"page {page} failed and no new buildId was available -- stopping"
            build_id = new_build_id
            continue  # retry the same page, once, under the fresh buildId

        page_hits = props.get("ssrHits") or []
        hits.extend(page_hits)
        if props.get("ssrIsLastPage") or not page_hits:
            break
        page += 1

    return hits, ""
