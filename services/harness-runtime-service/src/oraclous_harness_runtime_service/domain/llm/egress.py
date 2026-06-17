"""Outbound-URL egress guard for USER-supplied LLM base URLs (ORAA-4 §21 domain layer).

A custom BYOM connection may carry its own ``base_url`` (any OpenAI-compatible endpoint). That URL
is attacker-controllable, so before the runtime makes a request to it we validate it as an SSRF
egress guard:

  * always reject a non-http(s) scheme, a missing host, and the link-local / cloud-metadata range
    ``169.254.0.0/16`` (incl. 169.254.169.254) — never a legitimate LLM, pure SSRF;
  * when ``allow_private`` is False (multi-tenant), also reject loopback, RFC-1918 private ranges,
    IPv6 ULA (fc00::/7), and the literal host ``localhost``;
  * when ``allow_private`` is True (the trusted single-tenant default), allow private/loopback so a
    user's local LLM (``host.docker.internal`` / ``127.0.0.1`` / ``192.168.x``) works.

Operator-configured server-map base URLs are TRUSTED and are NOT run through this guard.

DNS-rebinding caveat: this is a resolve-time check — a hostname that resolves to a public IP here
could resolve to a private IP at request time (TOCTOU). A fuller fix pins the resolved IP into the
outbound request (connect to the validated address with the SNI/Host preserved). That is out of
scope here and not a blocker for the single-tenant default; multi-tenant deployments should layer
network egress controls in addition to this check.
"""

from __future__ import annotations

import ipaddress
import socket
from ipaddress import IPv4Address, IPv6Address
from urllib.parse import urlsplit

# ``ipaddress.ip_address()`` returns exactly one of these concrete types; the abstract
# ``ipaddress._BaseAddress`` base lacks ``is_private`` / ``is_loopback`` / ``is_link_local`` etc.
# (those live on the concrete classes), so the egress guards type against the concrete union.
_IPAddress = IPv4Address | IPv6Address

# RFC 3927 / RFC 4291 link-local — also covers the cloud metadata endpoint 169.254.169.254. ALWAYS
# blocked: there is no legitimate LLM here, only SSRF into instance metadata.
_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")
_LINK_LOCAL_V6 = ipaddress.ip_network("fe80::/10")


class EgressBlockedError(Exception):
    """A user-supplied outbound URL is blocked by the egress guard (a clear 4xx upstream)."""


def _resolve_ips(host: str) -> list[_IPAddress]:
    """Resolve a host to its IP(s). A bare IP literal short-circuits the DNS lookup."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise EgressBlockedError(f"cannot resolve host {host!r}: {exc}") from exc
    addrs: list[_IPAddress] = []
    for info in infos:
        sockaddr = info[4]
        try:
            addrs.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    if not addrs:
        raise EgressBlockedError(f"host {host!r} resolved to no usable address")
    return addrs


def _is_link_local(ip: _IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv4Address):
        return ip in _LINK_LOCAL_V4
    return ip in _LINK_LOCAL_V6 or ip.is_link_local


def _is_private(ip: _IPAddress) -> bool:
    """Loopback (127/8, ::1), RFC-1918 (10/8, 172.16/12, 192.168/16), and IPv6 ULA (fc00::/7)."""
    return ip.is_loopback or ip.is_private


def _normalize(ip: _IPAddress) -> _IPAddress:
    """An IPv4-mapped IPv6 (``::ffff:a.b.c.d``) routes to the IPv4 address — return that, so the
    link-local/private rules cannot be bypassed by mapping a blocked IPv4 (e.g. the metadata IP)
    into IPv6 on a Python whose ``is_link_local`` does not delegate the mapping."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ipaddress.ip_address(ip.ipv4_mapped)
    return ip


def validate_outbound_url(url: str, *, allow_private: bool) -> None:
    """Raise :class:`EgressBlockedError` if ``url`` is an unsafe outbound target.

    Args:
        url: the user-supplied base URL (e.g. ``https://my-endpoint/v1``).
        allow_private: when True (trusted single-tenant default), private/loopback targets are
            allowed so a user's local LLM works; the link-local/metadata range is still blocked.
            When False (multi-tenant), private/loopback/``localhost`` are also blocked.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise EgressBlockedError(
            f"base_url scheme {parts.scheme or '(none)'!r} is not allowed; use http(s)"
        )
    host = parts.hostname
    if not host:
        raise EgressBlockedError("base_url has no host")

    # The literal `localhost` is private-by-name; block it directly in multi-tenant mode (it may
    # resolve to 127.0.0.1 OR ::1, and we want one clear message rather than a per-IP one).
    if not allow_private and host.lower() == "localhost":
        raise EgressBlockedError("base_url host 'localhost' is not allowed in this deployment")

    for ip in _resolve_ips(host):
        ip = _normalize(ip)  # an IPv4-mapped IPv6 routes to its IPv4 address — check it as IPv4
        # ALWAYS blocked, in either mode — cloud-metadata SSRF.
        if _is_link_local(ip):
            raise EgressBlockedError(
                f"base_url host {host!r} resolves to link-local/metadata address {ip} (blocked)"
            )
        if not allow_private and _is_private(ip):
            raise EgressBlockedError(
                f"base_url host {host!r} resolves to private/loopback address {ip} "
                "(blocked in this deployment)"
            )
