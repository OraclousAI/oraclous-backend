"""SSRF egress guard (domain layer) — pure URL/IP classification for outbound calls.

An imported external MCP server URL is attacker-influenced; before the registry calls it we reject
any URL that targets our own infrastructure. ``is_public_url`` is a PURE structural gate (scheme +
literal-IP classification + hostname denylist + single-label-host block); the executor ADDITIONALLY
resolves the hostname and re-checks each resolved IP with ``is_private_ip`` (catching a public
hostname that points inward). KNOWN LIMITATIONS (same recorded follow-on — connect to the
resolved+vetted IP, not the hostname): a DNS-rebinding TOCTOU between the resolve-check and the
connect; and a dotted alternate-radix host (e.g. ``0177.0.0.1``) whose safety rests on the resolver
NOT octal-decoding it (glibc/macOS do not — it lands on a public IP, not localhost).
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlparse

_BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata", "metadata.google.internal"})
_BLOCKED_SUFFIXES = (".internal", ".local", ".localhost", ".cluster.local")
_ALLOW_PRIVATE_ENV = "CAPABILITY_REGISTRY_ALLOW_PRIVATE_EGRESS"


def _env_allow_private() -> bool:
    """The service-wide single-tenant egress relaxation knob (mirrors KGS
    ``sql_ingest_allow_private_egress`` / HRS ``allow_private_llm_targets``). Default OFF =
    multi-tenant safe. When ON, a deploy trusts private/loopback + single-label container targets
    (e.g. a same-stack gitea forge for the deliver-back e2e) — but cloud-metadata/IMDS + link-local
    stay blocked in EITHER mode (``allow_private`` NEVER relaxes them)."""
    return os.environ.get(_ALLOW_PRIVATE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _ip_always_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """The IPs no mode ever allows: link-local (169.254 IMDS / fe80::), multicast, reserved,
    unspecified. ``allow_private`` only relaxes RFC-1918 private + loopback, never these."""
    return ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified


def is_private_ip(ip_str: str) -> bool:
    """True if ``ip_str`` is loopback/private/link-local/reserved/multicast/unspecified — never a
    valid external target. Fail-closed: an unparseable value is treated as unsafe (True)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 169.254/16 — incl. the cloud metadata endpoint
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def is_public_url(url: str, *, allow_private: bool = False) -> bool:
    """Pure structural gate, fail-closed. Requires an http(s) scheme + a host that is not localhost,
    not a literal private/loopback/link-local IP, not ``*.internal``/``.local``/etc., and not a
    single-label host (a bare ``postgres`` is an internal container name; a real external server is
    an FQDN). A real FQDN passes here; the executor then resolves + re-checks the resolved IPs.

    ``allow_private`` (the single-tenant relaxation) lets a private/loopback IP or a single-label
    container name through, BUT cloud-metadata/IMDS + link-local are blocked in either mode."""
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in _BLOCKED_HOSTNAMES:  # localhost/metadata — never a target, in either mode
        return False
    try:
        ip = ipaddress.ip_address(host)  # a literal IP host — classify it directly
    except ValueError:
        pass
    else:
        if _ip_always_blocked(ip):  # IMDS/link-local/multicast/reserved — never
            return False
        if ip.is_private or ip.is_loopback:
            return allow_private  # RFC-1918 / loopback only in single-tenant mode
        return True
    if "." not in host or any(host.endswith(suffix) for suffix in _BLOCKED_SUFFIXES):
        return allow_private  # a single-label container name / *.internal — single-tenant only
    return True


def _resolved_ip_ok(ip_str: str, *, allow_private: bool) -> bool:
    """A resolved IP is acceptable: fail-closed on an unparseable value; IMDS/link-local always
    blocked; private/loopback only in ``allow_private`` mode."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if _ip_always_blocked(ip):
        return False
    if ip.is_private or ip.is_loopback:
        return allow_private
    return True


async def egress_allowed(url: str, *, allow_private: bool | None = None) -> bool:
    """The full egress gate (the shared SSRF check for every outbound call — invoke AND import):
    ``is_public_url`` (pure) PLUS, for a hostname, an async DNS resolve re-checking EVERY resolved
    IP, so a public name pointing inward is blocked. Fail-closed on an unresolvable host.

    ``allow_private`` defaults to the service-wide ``CAPABILITY_REGISTRY_ALLOW_PRIVATE_EGRESS`` env
    knob (None → read it) so callers (e.g. the github-sink) need not thread it; IMDS stays blocked.
    """
    if allow_private is None:
        allow_private = _env_allow_private()
    if not is_public_url(url, allow_private=allow_private):
        return False
    host = urlparse(url).hostname or ""
    try:
        ipaddress.ip_address(host)  # a literal-IP host was already cleared by is_public_url
    except ValueError:
        pass
    else:
        return True
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return False
    return bool(infos) and all(
        _resolved_ip_ok(info[4][0], allow_private=allow_private) for info in infos
    )
