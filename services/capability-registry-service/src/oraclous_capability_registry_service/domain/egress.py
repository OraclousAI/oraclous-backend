"""SSRF egress guard (ORAA-4 §21 domain layer) — pure URL/IP classification for outbound calls.

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

import ipaddress
from urllib.parse import urlparse

_BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata", "metadata.google.internal"})
_BLOCKED_SUFFIXES = (".internal", ".local", ".localhost", ".cluster.local")


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


def is_public_url(url: str) -> bool:
    """Pure structural gate, fail-closed. Requires an http(s) scheme + a host that is not localhost,
    not a literal private/loopback/link-local IP, not ``*.internal``/``.local``/etc., and not a
    single-label host (a bare ``postgres`` is an internal container name; a real external server is
    an FQDN). A real FQDN passes here; the executor then resolves + re-checks the resolved IPs."""
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    try:
        ipaddress.ip_address(host)  # a literal IP host — classify it directly
    except ValueError:
        pass
    else:
        return not is_private_ip(host)
    if host in _BLOCKED_HOSTNAMES or "." not in host:
        return False
    return not any(host.endswith(suffix) for suffix in _BLOCKED_SUFFIXES)
