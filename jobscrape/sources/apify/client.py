"""Apify REST API v2: runs and dataset items.

Thin on purpose — it goes through the shared :class:`~jobscrape.http.Fetcher`,
so retries, per-host rate limiting and the disk cache all apply without being
reimplemented here.

Caching is disabled for every call. A dataset is written once and read once, and
a run's status is the thing being checked; serving either from a six-hour cache
would mean ingesting stale items or trusting a stale status.
"""

from __future__ import annotations

import logging
from typing import Any

from ... import config
from ...http import Fetcher

log = logging.getLogger("jobscrape.apify")


def available() -> tuple[bool, str]:
    """Whether an API token is configured, and what to do if not."""
    if not config.APIFY_TOKEN:
        return False, "set APIFY_TOKEN (apify.com -> Settings -> Integrations)"
    return True, ""


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.APIFY_TOKEN}"}


async def run_info(fetcher: Fetcher, run_id: str) -> dict[str, Any] | None:
    """Fetch a run, or ``None`` if it cannot be read.

    The webhook payload already contains a `defaultDatasetId`, and this exists
    so that it is never trusted. The payload is unauthenticated input — Apify
    offers no request signing — so a forged body could otherwise name any
    dataset on the account and have its contents ingested. Reading the run back
    means the dataset id comes from the API, and the run's real status is
    checked rather than taken from the caller.
    """
    data = await fetcher.get_json(
        f"{config.APIFY_API}/actor-runs/{run_id}",
        headers=_headers(),
        cache=False,
    )
    if not isinstance(data, dict):
        return None
    # Apify wraps single resources in {"data": {...}}.
    inner = data.get("data")
    return inner if isinstance(inner, dict) else data


async def actor_name(fetcher: Fetcher, actor: str) -> str | None:
    """The actor's readable name, given its opaque id.

    A run reports `actId` as an id like `nlZZi3lZre4fM9IET`, which says nothing
    about which schema its dataset follows. This is the lookup that turns it
    back into `indeed-job-scraper`.

    Public for a public actor, so the token is sent but not required — an actor
    that 404s here simply yields ``None`` and the caller declines to map it.
    Cached: an actor's name does not change within a run, and this would
    otherwise be one request per webhook delivery.
    """
    data = await fetcher.get_json(
        f"{config.APIFY_API}/acts/{actor}",
        headers=_headers() if config.APIFY_TOKEN else None,
    )
    if not isinstance(data, dict):
        return None
    inner = data.get("data")
    inner = inner if isinstance(inner, dict) else data
    name = inner.get("name")
    username = inner.get("username")
    if isinstance(name, str) and name:
        return f"{username}~{name}" if isinstance(username, str) and username else name
    return None


async def dataset_items(
    fetcher: Fetcher, dataset_id: str, max_items: int | None = None
) -> list[dict[str, Any]]:
    """Every item in a dataset, paginated.

    Apify enforces no maximum page size, but an unbounded request would hold a
    whole run in memory at once; `APIFY_PAGE_SIZE` bounds it. Pagination stops
    on a short page, which is the only reliable end-of-data signal — the
    `X-Apify-Pagination-Total` header is not exposed through `get_json`.
    """
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page_size = config.APIFY_PAGE_SIZE
        if max_items is not None:
            remaining = max_items - len(items)
            if remaining <= 0:
                break
            page_size = min(page_size, remaining)
        batch = await fetcher.get_json(
            f"{config.APIFY_API}/datasets/{dataset_id}/items",
            params={"offset": offset, "limit": page_size, "clean": "true"},
            headers=_headers(),
            cache=False,
        )
        if not isinstance(batch, list):
            log.warning("apify: dataset %s returned %s, not a list",
                        dataset_id, type(batch).__name__)
            break
        items.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < page_size:
            break
        offset += len(batch)
    return items


async def last_runs(
    fetcher: Fetcher, actor: str, limit: int = 5
) -> list[dict[str, Any]]:
    """The most recent succeeded runs of an actor, newest first.

    Used by `apify-pull` to recover a webhook delivery that never arrived. Only
    succeeded runs are returned, since a failed run's dataset is partial at best.
    """
    data = await fetcher.get_json(
        f"{config.APIFY_API}/acts/{actor}/runs",
        params={"desc": "true", "limit": limit, "status": "SUCCEEDED"},
        headers=_headers(),
        cache=False,
    )
    if not isinstance(data, dict):
        return []
    inner = data.get("data")
    items = inner.get("items") if isinstance(inner, dict) else None
    return [r for r in (items or []) if isinstance(r, dict)]
