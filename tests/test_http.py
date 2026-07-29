"""Tests for the shared client's rate limiting and caching.

A board lost to rate limiting is a board whose jobs never reach the dataset, so
the throttling logic is a correctness concern rather than mere courtesy.
"""

from __future__ import annotations

import asyncio

import pytest

from jobscrape import config
from jobscrape.http import Fetcher, gather_limited


async def test_host_overrides_are_stricter_than_defaults() -> None:
    async with Fetcher() as fetcher:
        limiter = fetcher._hosts
        default = limiter._settings_for("example.com")
        ashby = limiter._settings_for("jobs.ashbyhq.com")
        assert ashby[0] <= default[0], "override must not raise concurrency"
        assert ashby[1] >= default[1], "override must not shorten the delay"


async def test_user_setting_is_never_loosened_by_override() -> None:
    """An explicit tighter setting must win over the built-in override."""
    async with Fetcher(per_host=1, delay=2.0) as fetcher:
        concurrency, delay = fetcher._hosts._settings_for("jobs.ashbyhq.com")
        assert concurrency == 1
        assert delay == 2.0


async def test_slow_down_increases_delay_and_caps() -> None:
    async with Fetcher() as fetcher:
        limiter = fetcher._hosts
        host = "jobs.ashbyhq.com"
        limiter._for(host)
        start = limiter._delays[host]
        limiter.slow_down(host)
        assert limiter._delays[host] > start
        for _ in range(50):
            limiter.slow_down(host)
        assert limiter._delays[host] <= 8.0, "backoff must be bounded"


async def test_slow_down_is_per_host() -> None:
    async with Fetcher() as fetcher:
        limiter = fetcher._hosts
        limiter._for("a.example"), limiter._for("b.example")
        limiter.slow_down("a.example")
        assert limiter._delays["a.example"] > limiter._delays["b.example"]


async def test_per_host_delay_is_enforced() -> None:
    """Consecutive acquisitions on one host must be spaced by the delay."""
    async with Fetcher(per_host=1, delay=0.05) as fetcher:
        limiter = fetcher._hosts
        loop = asyncio.get_running_loop()
        start = loop.time()
        for _ in range(3):
            sem = await limiter.acquire("slow.example")
            sem.release()
        assert loop.time() - start >= 0.09  # at least two gaps of 0.05s


async def test_gather_limited_isolates_failures() -> None:
    """One failing task must not abort the batch — a bad board can't stop a sweep."""

    async def ok(value: int) -> int:
        return value

    async def boom() -> int:
        raise RuntimeError("board exploded")

    results = await gather_limited([ok(1), boom(), ok(3)], limit=2)
    assert results == [1, None, 3]


async def test_gather_limited_respects_limit() -> None:
    active = 0
    peak = 0

    async def task() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    await gather_limited([task() for _ in range(10)], limit=3)
    assert peak <= 3


async def test_cache_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    async with Fetcher(use_cache=True) as fetcher:
        path = fetcher._cache_path("GET", "https://x.example/a", None)
        fetcher._cache_write(path, {"data": {"hello": "world"}})
        assert fetcher._cache_read(path) == {"data": {"hello": "world"}}


async def test_cache_miss_on_different_body(tmp_path, monkeypatch) -> None:
    """POST sources (Workday, Ashby) must key the cache on the request body."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    async with Fetcher(use_cache=True) as fetcher:
        a = fetcher._cache_path("POST", "https://x.example/q", {"offset": 0})
        b = fetcher._cache_path("POST", "https://x.example/q", {"offset": 20})
        assert a != b


async def test_expired_cache_is_ignored(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    async with Fetcher(use_cache=True, cache_ttl=1) as fetcher:
        path = fetcher._cache_path("GET", "https://x.example/b", None)
        fetcher._cache_write(path, {"data": 1})
        import os, time
        old = time.time() - 60
        os.utime(path, (old, old))
        assert fetcher._cache_read(path) is None


async def test_cache_key_separates_text_and_json(tmp_path, monkeypatch) -> None:
    """A get_text() must not poison a later get_json() on the same URL.

    Before `expect` was part of the cache key, the cached value was the raw
    string, so get_json() handed a str back to a caller expecting a dict and the
    adapter silently parsed nothing.
    """
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    async with Fetcher(use_cache=True) as fetcher:
        url = "https://x.example/board"
        text_key = fetcher._cache_path("GET", url, None, "text")
        json_key = fetcher._cache_path("GET", url, None, "json")
        assert text_key != json_key

        fetcher._cache_write(text_key, {"data": '{"jobs": []}'})
        # The JSON slot is still empty, so a JSON caller must not see the string.
        assert fetcher._cache_read(json_key) is None
        assert fetcher._cache_read(text_key) == {"data": '{"jobs": []}'}


async def test_shared_ats_hosts_get_extra_retries() -> None:
    """A board lost to a 429 is a board whose jobs never arrive."""
    assert "jobs.ashbyhq.com" in config.HOST_LIMITS
    assert config.MAX_RETRIES <= 6
