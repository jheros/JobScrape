"""Shared async HTTP client: per-host rate limiting, retries, disk cache.

Every network call in this project goes through :class:`Fetcher` so that
politeness limits are enforced in exactly one place.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from . import config

log = logging.getLogger("jobscrape.http")

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class _HostLimiter:
    """One semaphore + minimum-interval gate per host.

    Hosts listed in :data:`jobscrape.config.HOST_LIMITS` get their own stricter
    settings; everything else uses the defaults.
    """

    def __init__(self, concurrency: int, delay: float) -> None:
        self._concurrency = concurrency
        self._delay = delay
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._delays: dict[str, float] = {}
        self._last: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = {}

    def _settings_for(self, host: str) -> tuple[int, float]:
        override = config.HOST_LIMITS.get(host)
        if override:
            # Never loosen below an explicit user setting.
            return min(override[0], self._concurrency), max(override[1], self._delay)
        return self._concurrency, self._delay

    def _for(self, host: str) -> tuple[asyncio.Semaphore, asyncio.Lock]:
        if host not in self._sems:
            concurrency, delay = self._settings_for(host)
            self._sems[host] = asyncio.Semaphore(concurrency)
            self._delays[host] = delay
            self._locks[host] = asyncio.Lock()
        return self._sems[host], self._locks[host]

    async def acquire(self, host: str) -> asyncio.Semaphore:
        sem, lock = self._for(host)
        await sem.acquire()
        delay = self._delays.get(host, self._delay)
        async with lock:
            elapsed = time.monotonic() - self._last[host]
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)
            self._last[host] = time.monotonic()
        return sem

    def slow_down(self, host: str, factor: float = 1.5, cap: float = 8.0) -> None:
        """Increase a host's delay after it rate-limits us.

        Backing off only the individual request is not enough when hundreds of
        boards share one hostname — the whole host has to ease off, and stay
        eased off for the rest of the run.
        """
        self._for(host)
        self._delays[host] = min(self._delays.get(host, self._delay) * factor, cap)


class Fetcher:
    """Async HTTP wrapper with caching, backoff and concurrency control."""

    def __init__(
        self,
        *,
        concurrency: int | None = None,
        per_host: int | None = None,
        delay: float | None = None,
        use_cache: bool | None = None,
        cache_ttl: int | None = None,
    ) -> None:
        self._global = asyncio.Semaphore(concurrency or config.GLOBAL_CONCURRENCY)
        self._hosts = _HostLimiter(
            per_host or config.PER_HOST_CONCURRENCY,
            config.PER_HOST_DELAY if delay is None else delay,
        )
        self._use_cache = config.CACHE_ENABLED if use_cache is None else use_cache
        self._cache_ttl = cache_ttl if cache_ttl is not None else config.CACHE_TTL_SECONDS
        self._client: httpx.AsyncClient | None = None
        self.stats = defaultdict(int)

    async def __aenter__(self) -> "Fetcher":
        config.ensure_dirs()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.HTTP_TIMEOUT),
            follow_redirects=True,
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # -- cache -------------------------------------------------------------
    def _cache_path(self, method: str, url: str, body: Any, expect: str = "json") -> Path:
        # `expect` is part of the key: the same URL fetched as text and as JSON
        # yields different value types, and without this a get_text() would
        # hand a raw string back to a later get_json() caller.
        key = hashlib.sha256(
            f"{method}|{url}|{expect}|"
            f"{json.dumps(body, sort_keys=True, default=str) if body else ''}".encode()
        ).hexdigest()
        return config.CACHE_DIR / key[:2] / f"{key}.json"

    def _cache_read(self, path: Path, ttl: int | None = None) -> Any | None:
        if not self._use_cache or not path.exists():
            return None
        ttl = self._cache_ttl if ttl is None else ttl
        if ttl > 0 and time.time() - path.stat().st_mtime > ttl:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _cache_write(self, path: Path, payload: Any) -> None:
        if not self._use_cache:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    # -- core --------------------------------------------------------------
    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        expect: str = "json",
        cache: bool | None = None,
        cache_ttl: int | None = None,
        retries: int | None = None,
    ) -> Any | None:
        """Perform a request, returning parsed JSON, text, or ``None`` on failure.

        A ``None`` return means "this source had nothing for us" and is always
        recoverable — callers skip and continue rather than aborting a sweep.
        """
        if self._client is None:
            raise RuntimeError("Fetcher must be used as an async context manager")

        full_url = str(httpx.URL(url, params=params)) if params else url
        cache_key = self._cache_path(method, full_url, json_body, expect)
        use_cache = self._use_cache if cache is None else cache
        if use_cache:
            # A per-call TTL lets a quota-limited source (JSearch) hold its
            # responses far longer than the global default.
            hit = self._cache_read(cache_key, cache_ttl)
            if hit is not None:
                self.stats["cache_hit"] += 1
                return hit.get("data")

        host = urlparse(full_url).netloc
        attempts = config.MAX_RETRIES if retries is None else retries
        # A board lost to rate limiting is a board whose jobs never appear, so
        # give shared ATS hosts more attempts than the generic default.
        if retries is None and host in config.HOST_LIMITS:
            attempts = max(attempts, 6)
        last_error: str | None = None

        async with self._global:
            sem = await self._hosts.acquire(host)
            try:
                for attempt in range(attempts + 1):
                    try:
                        resp = await self._client.request(
                            method, full_url, json=json_body, headers=headers
                        )
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        last_error = f"{type(exc).__name__}: {exc}"
                        if attempt >= attempts:
                            break
                        await self._backoff(attempt)
                        continue

                    if resp.status_code == 429:
                        # Slow the whole host, not just this request: hundreds
                        # of boards share one hostname, so per-request backoff
                        # alone lets the next board immediately re-trip the limit.
                        self._hosts.slow_down(host)
                    if resp.status_code in RETRY_STATUS and attempt < attempts:
                        self.stats[f"retry_{resp.status_code}"] += 1
                        await self._backoff(attempt, resp.headers.get("Retry-After"))
                        continue

                    if resp.status_code >= 400:
                        self.stats[f"http_{resp.status_code}"] += 1
                        log.debug("%s %s -> %s", method, full_url, resp.status_code)
                        return None

                    self.stats["ok"] += 1
                    try:
                        data = resp.json() if expect == "json" else resp.text
                    except (ValueError, json.JSONDecodeError):
                        if expect == "json":
                            self.stats["bad_json"] += 1
                            return None
                        data = resp.text
                    self._cache_write(cache_key, {"data": data})
                    return data
            finally:
                sem.release()

        self.stats["failed"] += 1
        if last_error:
            log.debug("%s %s failed: %s", method, full_url, last_error)
        return None

    async def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                # Capped at 30s: a server is free to ask for a five-minute wait,
                # and honouring that literally lets one rate-limited board stall
                # a sweep of thousands. The host-wide slow_down() above is what
                # actually keeps us polite past this point.
                await asyncio.sleep(min(float(retry_after), 30.0))
                return
            except ValueError:
                pass
        # Exponential with jitter so parallel workers don't resynchronize.
        await asyncio.sleep(min(2**attempt + random.random(), 30.0))

    async def get_json(self, url: str, **kw: Any) -> Any | None:
        return await self.request("GET", url, **kw)

    async def post_json(self, url: str, body: Any, **kw: Any) -> Any | None:
        return await self.request("POST", url, json_body=body, **kw)

    async def get_text(self, url: str, **kw: Any) -> str | None:
        kw["expect"] = "text"
        return await self.request("GET", url, **kw)


async def gather_limited(coros: list[Any], limit: int = 16) -> list[Any]:
    """Run coroutines with a cap, returning results positionally.

    Exceptions become ``None`` so one bad company never kills a whole sweep.
    """
    sem = asyncio.Semaphore(limit)

    async def _run(coro: Any) -> Any:
        async with sem:
            try:
                return await coro
            except Exception as exc:  # noqa: BLE001 - isolation is the point
                log.debug("task failed: %s", exc)
                return None

    return await asyncio.gather(*(_run(c) for c in coros))
