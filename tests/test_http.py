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


# --- probe(): validated-address pinning (SSRF / DNS-rebinding TOCTOU) ------


class _RecordingClient:
    """Stands in for httpx.AsyncClient — records every request() call and
    plays back a queued response, without ever touching the network."""

    def __init__(self, responses):
        self.calls: list[tuple[str, str, dict]] = []
        self._responses = list(responses)

    async def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self._responses.pop(0)

    async def aclose(self) -> None:
        pass


def _response(status: int, *, url: str, text: str = "", location: str | None = None):
    import httpx
    headers = {"location": location} if location else {}
    return httpx.Response(status, headers=headers, text=text,
                           request=httpx.Request("GET", url))


async def test_probe_dials_the_validated_ip_not_the_hostname() -> None:
    """`validate` returns the one address it confirmed public; probe() must
    connect to THAT address rather than handing the hostname to the client,
    which would resolve DNS again at connect time -- an independent lookup
    with a rebinding window between the two (see safehttp's module docstring)."""
    client = _RecordingClient([
        _response(200, url="https://93.184.216.34/job/1", text="hello"),
    ])
    async with Fetcher() as fetcher:
        fetcher._client = client
        result = await fetcher.probe(
            "https://totally-innocent.example.com/job/1",
            validate=lambda u: "93.184.216.34",
        )

    assert result == (200, "https://totally-innocent.example.com/job/1", "hello")
    method, url, kw = client.calls[0]
    assert url.startswith("https://93.184.216.34"), url
    assert kw["headers"]["Host"] == "totally-innocent.example.com"
    assert kw["extensions"]["sni_hostname"] == "totally-innocent.example.com"


async def test_probe_does_not_set_sni_hostname_for_plain_http() -> None:
    """SNI is a TLS concept -- setting it for a plain http:// request is at
    best a no-op and shouldn't be asserted as if it mattered there."""
    client = _RecordingClient([
        _response(200, url="http://93.184.216.34/job/1", text="hello"),
    ])
    async with Fetcher() as fetcher:
        fetcher._client = client
        await fetcher.probe(
            "http://totally-innocent.example.com/job/1",
            validate=lambda u: "93.184.216.34",
        )

    _, _, kw = client.calls[0]
    assert kw["headers"]["Host"] == "totally-innocent.example.com"
    assert "sni_hostname" not in kw["extensions"]


async def test_probe_redirect_is_resolved_against_the_hostname_not_the_pinned_ip() -> None:
    """The physical request went to an IP; joining a relative Location against
    that would produce a next hop with the IP as its "hostname" instead of
    resolving relative to the real host the redirect belongs to."""
    client = _RecordingClient([
        _response(302, url="https://93.184.216.34/a", location="/b"),
        _response(200, url="https://93.184.216.34/b", text="done"),
    ])
    async with Fetcher() as fetcher:
        fetcher._client = client
        result = await fetcher.probe(
            "https://real-host.example.com/a",
            validate=lambda u: "93.184.216.34",
        )

    assert result == (200, "https://real-host.example.com/b", "done")
    # Second hop must also have been validated and pinned again, against the
    # real next-hop hostname URL, not re-resolved from the IP.
    assert len(client.calls) == 2
    assert client.calls[1][1].startswith("https://93.184.216.34")
    assert client.calls[1][2]["headers"]["Host"] == "real-host.example.com"


async def test_probe_without_validate_still_works_unpinned() -> None:
    """Only the untrusted-URL caller (liveness) passes `validate`; every other
    use of probe() (currently none, but the parameter is optional) must keep
    working with no pinning at all."""
    client = _RecordingClient([
        _response(200, url="https://trusted.example.com/x", text="ok"),
    ])
    async with Fetcher() as fetcher:
        fetcher._client = client
        result = await fetcher.probe("https://trusted.example.com/x")

    assert result == (200, "https://trusted.example.com/x", "ok")
    _, url, kw = client.calls[0]
    assert url == "https://trusted.example.com/x"
    assert kw["headers"] == {}
