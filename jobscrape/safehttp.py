"""SSRF guard for probing URLs we did not choose.

Every other request this app makes goes to an endpoint *we* named — an ATS API,
a known aggregator. The liveness probe is different: it fetches
``jobs.job_url``, a URL that arrived in third-party data. A hostile or merely
broken one can point at the loopback interface, a private subnet, or a cloud
metadata endpoint, and a plain HTTP client would happily fetch it.

The guard resolves the hostname and rejects the *resolved addresses*, not the
name. Checking the name is not equivalent: ``evil.example.com`` can resolve to
``127.0.0.1``, and a name that resolves publicly once can resolve privately on
the next lookup (DNS rebinding). Redirects are re-checked hop by hop for the
same reason — the first response is under the remote server's control.

**Validating is not enough on its own.** A validated hostname still has to be
turned into a TCP connection somehow, and if that second step re-resolves the
name itself (which a plain HTTP client does, at connect time), the address
`check_url` just approved is not the address the request actually goes to —
two independent DNS lookups with an attacker-controlled window in between,
the exact TOCTOU form of rebinding the module-level check above already
warns about. `check_url` therefore *returns* the address it validated, and
callers are expected to connect to that literal address (with the hostname
preserved only for the Host header / TLS SNI) rather than handing the
hostname to the transport and letting it resolve again. See `http.Fetcher.probe`
for the caller that does this.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

#: Carrier-grade NAT. `ipaddress.is_private` does NOT cover this range, and
#: AWS/GCP use it for internal addressing that is reachable from an instance —
#: so omitting it leaves exactly the hole this module exists to close.
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
#: The IPv6 equivalent of the cloud metadata / link-local space.
_V6_UNIQUE_LOCAL = ipaddress.ip_network("fc00::/7")


class BlockedAddress(Exception):
    """Raised when a URL resolves to an address we refuse to contact."""


def is_blocked_ip(ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Is this address one we must never fetch from?

    Fails **closed**: anything unparseable is blocked, because "I could not
    tell what this is" is not a reason to connect to it.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True

    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        # ::ffff:127.0.0.1 is loopback wearing a v6 hat.
        addr = addr.ipv4_mapped

    return (
        addr.is_loopback           # 127.0.0.0/8, ::1
        or addr.is_private         # 10/8, 172.16/12, 192.168/16, fd00::/8
        or addr.is_link_local      # 169.254.0.0/16 — cloud metadata lives here
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified     # 0.0.0.0, ::
        or (addr.version == 4 and addr in _CGNAT_V4)
        or (addr.version == 6 and addr in _V6_UNIQUE_LOCAL)
    )


def resolve_public(host: str, port: int = 443) -> list[str]:
    """Resolve ``host`` and return its addresses, or raise :class:`BlockedAddress`.

    **Every** returned address must be public. A name that resolves to one
    public and one private address is rejected outright rather than filtered:
    which address the client would actually connect to is not ours to control,
    so partial safety is no safety.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise BlockedAddress(f"cannot resolve {host!r}: {exc}") from exc

    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise BlockedAddress(f"{host!r} resolved to nothing")
    for address in addresses:
        if is_blocked_ip(address):
            raise BlockedAddress(f"{host!r} resolves to non-public address {address}")
    return addresses


def check_url(url: str) -> str:
    """Raise :class:`BlockedAddress` unless ``url`` is safe to fetch; return
    the validated address to connect to.

    Rejects non-HTTP schemes outright — ``file://``, ``gopher://`` and friends
    have no business here and are a classic SSRF pivot.

    The return value matters as much as the raise: it is the one address, of
    however many `resolve_public` found, that the caller should actually dial
    (see the module docstring). Always the first result — every returned
    address is independently confirmed public, so any one of them is safe;
    picking deterministically just keeps repeated probes of the same URL
    landing on the same address instead of whichever the loopback OS resolver
    felt like returning first.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BlockedAddress(f"refusing scheme {parsed.scheme!r}")
    if not parsed.hostname:
        raise BlockedAddress("no host in URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return resolve_public(parsed.hostname, port)[0]
