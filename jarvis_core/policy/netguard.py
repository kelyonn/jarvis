"""Fetching a URL the model chose, without letting it reach your own machine or network.

A web page, email or message can tell the model to fetch ``http://127.0.0.1:8080/admin`` or the cloud metadata address
``169.254.169.254``. Checking only the first URL is not enough, because a public page can redirect to a private one. So
every hop is resolved and checked, redirects are followed by hand, and the body is size-capped.

Known limit: the name is resolved once for the check and again by the HTTP client. A hostile DNS server could answer
differently the second time (DNS rebinding). Closing that needs connecting to the checked address directly; for a
voice assistant fetching public pages, per-hop checking covers the realistic cases.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from jarvis_core.errors import SkillError

MAX_REDIRECTS = 5
MAX_BYTES = 1_500_000
Resolver = Callable[..., list[Any]]


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def host_is_public(host: str, resolver: Resolver = socket.getaddrinfo) -> bool:
    """True only if the host is an address, or resolves to addresses, that are all on the public internet."""
    host = host.strip("[]")
    try:
        return _ip_is_public(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = resolver(host, None)
    except OSError:
        return False
    addrs = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(info[4][0]))
        except (ValueError, IndexError):
            return False
    return bool(addrs) and all(_ip_is_public(a) for a in addrs)


def check_public_url(url: str, resolver: Resolver = socket.getaddrinfo) -> str:
    """Return the URL if it is a plain web link to a public host. Otherwise raise a spoken-friendly error."""
    p = urlparse(url.strip())
    if p.scheme not in {"http", "https"} or not p.hostname:
        raise SkillError("I can only read web pages, sir.")
    if p.username or p.password:
        raise SkillError("I won't open a link with a login in it, sir.")
    if not host_is_public(p.hostname, resolver):
        raise SkillError("That address is on this computer or your own network, so I won't open it, sir.")
    return p.geturl()


def fetch_public(
    url: str,
    *,
    timeout: float = 15.0,
    max_bytes: int = MAX_BYTES,
    resolver: Resolver = socket.getaddrinfo,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """GET a public page and return its text. Every redirect target is checked before it is followed."""
    current = check_public_url(url, resolver)
    with httpx.Client(follow_redirects=False, timeout=timeout, transport=transport,
                      headers={"User-Agent": "Mozilla/5.0"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            with client.stream("GET", current) as r:
                if r.is_redirect and r.headers.get("location"):
                    current = check_public_url(urljoin(current, r.headers["location"]), resolver)
                    continue
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) >= max_bytes:
                        break
                return bytes(body[:max_bytes]).decode(r.encoding or "utf-8", errors="replace")
    raise SkillError("That link redirected too many times, sir.")
