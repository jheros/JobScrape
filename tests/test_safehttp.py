"""SSRF guard.

The liveness probe fetches `jobs.job_url`, which arrives in third-party data —
so unlike every other request this app makes, the target is not one we chose.
"""

from __future__ import annotations

import pytest

from jobscrape.safehttp import BlockedAddress, check_url, is_blocked_ip, resolve_public


@pytest.mark.parametrize("ip", [
    "127.0.0.1",          # loopback
    "10.0.0.1",           # RFC1918
    "172.16.5.4",         # RFC1918
    "192.168.1.1",        # RFC1918
    "169.254.169.254",    # cloud metadata — the classic SSRF target
    "100.64.0.1",         # CGNAT: ipaddress.is_private does NOT cover this
    "100.127.255.254",    # CGNAT upper edge
    "0.0.0.0",            # unspecified
    "224.0.0.1",          # multicast
    "::1",                # v6 loopback
    "fd00::1",            # v6 unique-local
    "fe80::1",            # v6 link-local
    "::ffff:127.0.0.1",   # v4-mapped loopback wearing a v6 hat
    "not-an-ip",          # fails closed
])
def test_blocked_addresses(ip: str) -> None:
    assert is_blocked_ip(ip) is True, ip


@pytest.mark.parametrize("ip", [
    "8.8.8.8",
    "1.1.1.1",
    "93.184.216.34",
    "100.63.255.255",     # one below the CGNAT range
    "100.128.0.0",        # one above the CGNAT range
    "2606:4700:4700::1111",
])
def test_public_addresses_are_allowed(ip: str) -> None:
    assert is_blocked_ip(ip) is False, ip


def test_cgnat_boundaries_are_exact() -> None:
    """100.64.0.0/10 is 100.64.0.0 - 100.127.255.255."""
    assert is_blocked_ip("100.64.0.0") is True
    assert is_blocked_ip("100.127.255.255") is True
    assert is_blocked_ip("100.63.255.255") is False
    assert is_blocked_ip("100.128.0.0") is False


# --- URL-level checks -------------------------------------------------------

def test_non_http_schemes_are_refused() -> None:
    for url in ("file:///etc/passwd", "gopher://x/", "ftp://x/", "data:text/plain,x"):
        with pytest.raises(BlockedAddress):
            check_url(url)


def test_url_with_no_host_is_refused() -> None:
    with pytest.raises(BlockedAddress):
        check_url("http:///nohost")


def test_localhost_url_is_refused() -> None:
    with pytest.raises(BlockedAddress):
        check_url("http://127.0.0.1:8000/jobs")


def test_metadata_url_is_refused() -> None:
    with pytest.raises(BlockedAddress):
        check_url("http://169.254.169.254/latest/meta-data/")


def test_a_name_resolving_to_a_private_address_is_refused(monkeypatch) -> None:
    """The guard checks the RESOLVED address, not the name — a public-looking
    hostname can resolve to loopback, which is how DNS rebinding works."""
    import jobscrape.safehttp as safe

    monkeypatch.setattr(
        safe.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(BlockedAddress, match="non-public"):
        check_url("https://totally-innocent.example.com/job/1")


def test_mixed_public_and_private_resolution_is_refused(monkeypatch) -> None:
    """Partial safety is no safety: we don't control which address the client
    would pick, so any private answer disqualifies the whole name."""
    import jobscrape.safehttp as safe

    monkeypatch.setattr(
        safe.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443)),
                         (2, 1, 6, "", ("10.0.0.5", 443))],
    )
    with pytest.raises(BlockedAddress):
        resolve_public("mixed.example.com")


def test_unresolvable_host_is_refused(monkeypatch) -> None:
    import socket as sock

    import jobscrape.safehttp as safe

    def boom(*a, **k):
        raise sock.gaierror("nope")

    monkeypatch.setattr(safe.socket, "getaddrinfo", boom)
    with pytest.raises(BlockedAddress, match="cannot resolve"):
        check_url("https://does-not-exist.invalid/job/1")


def test_public_url_passes(monkeypatch) -> None:
    import jobscrape.safehttp as safe

    monkeypatch.setattr(
        safe.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    check_url("https://boards.greenhouse.io/acme/jobs/1")  # must not raise


def test_check_url_returns_the_validated_address_to_connect_to(monkeypatch) -> None:
    """The return value is not incidental -- `http.Fetcher.probe` dials this
    exact address rather than handing the hostname to the client and letting
    it resolve DNS a second time (the TOCTOU form of rebinding)."""
    import jobscrape.safehttp as safe

    monkeypatch.setattr(
        safe.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    assert check_url("https://boards.greenhouse.io/acme/jobs/1") == "93.184.216.34"


def test_check_url_returns_the_first_of_several_validated_addresses(monkeypatch) -> None:
    import jobscrape.safehttp as safe

    monkeypatch.setattr(
        safe.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443)),
                         (2, 1, 6, "", ("93.184.216.35", 443))],
    )
    assert check_url("https://multi.example.com/") == "93.184.216.34"
