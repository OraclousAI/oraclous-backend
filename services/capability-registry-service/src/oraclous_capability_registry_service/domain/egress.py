"""SSRF egress guard (domain layer) — pure URL/IP classification for outbound calls.

An imported external MCP server URL is attacker-influenced; before the registry calls it we reject
any URL that targets our own infrastructure. ``is_public_url`` is a PURE structural gate (scheme +
literal-IP classification + hostname denylist + single-label-host block); ``egress_allowed``
ADDITIONALLY resolves the hostname, re-checks each resolved IP, and — #492 (ADR-025 §1) — RETURNS
the vetted IP so the caller connects to THAT pinned IP via ``pinned_request`` (Host header + TLS SNI
kept as the original hostname), per redirect hop. Connecting to the vetted IP closes the
DNS-rebinding TOCTOU (a low-TTL name answering public to the guard + private/IMDS to httpx's
independent connect-time re-resolve) — the same pin-the-resolved-IP mitigation the KGS raw-TCP path
already ships (``tcp_egress.validate_db_host`` → ``DbConnParams.pinned_ip``). REMAINING LIMIT: a
dotted alternate-radix host (e.g. ``0177.0.0.1``) whose safety rests on the resolver NOT
octal-decoding it (glibc/macOS do not — it lands on a public IP, not localhost).
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

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


async def egress_allowed(url: str, *, allow_private: bool | None = None) -> str | None:
    """The full egress gate (the shared SSRF check for every outbound call — invoke AND import).
    Returns the VETTED IP to CONNECT TO (#492 / ADR-025 §1), or ``None`` when the URL is blocked.

    ``is_public_url`` (pure) PLUS, for a hostname, an async DNS resolve re-checking EVERY resolved
    IP, so a public name pointing inward is blocked — fail-closed on an unresolvable host. For a
    literal-IP host it returns that IP; for a hostname it returns the first vetted resolved address
    (IPv4 preferred, so the pin is broadly dialable). The caller MUST dial the returned IP via
    ``pinned_request`` — connecting by hostname would re-resolve and re-open the rebinding TOCTOU.

    ``allow_private`` defaults to the service-wide ``CAPABILITY_REGISTRY_ALLOW_PRIVATE_EGRESS`` env
    knob (None → read it) so callers (e.g. the github-sink) need not thread it; IMDS stays blocked.
    """
    if allow_private is None:
        allow_private = _env_allow_private()
    if not is_public_url(url, allow_private=allow_private):
        return None
    host = urlparse(url).hostname or ""
    try:
        ipaddress.ip_address(host)  # a literal-IP host was already cleared by is_public_url
    except ValueError:
        pass
    else:
        return host  # dial the literal IP itself (no DNS, nothing to rebind)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return None
    resolved = [info[4][0] for info in infos]
    # fail-closed: EVERY resolved address must be safe (a dual answer can't smuggle one private IP
    # past the guard). Then pin ONE vetted address — prefer IPv4 (a v6-only pin may be undialable).
    if not resolved or not all(_resolved_ip_ok(ip, allow_private=allow_private) for ip in resolved):
        return None
    return next((ip for ip in resolved if ":" not in ip), resolved[0])


def pinned_request(url: str, pinned_ip: str) -> tuple[httpx.URL, dict[str, str], dict[str, Any]]:
    """#492: build the (URL, headers, extensions) to CONNECT to ``pinned_ip`` while the request
    targets the original hostname. Swap the URL host to the vetted IP (so httpx dials THAT, no
    re-resolution), set the ``Host`` header to the original ``host[:port]`` (routing/vhosts), and
    pass ``sni_hostname`` so TLS SNI + certificate validation still target the real name (https
    only). Mirrors the KGS pinned-IP dial (``sql_connector`` host=pinned_ip, name for SNI)."""
    original = httpx.URL(url)
    host = original.host
    # an IPv6 literal must be bracketed in the Host header (RFC 3986) — httpx.URL.host strips the
    # brackets, so a bare "::1:8080" would be ambiguous; re-bracket before appending any port (L6).
    host_label = f"[{host}]" if ":" in host else host
    # the Host header carries the original name; include the port only when it was explicit/non-
    # default (httpx omits a default 80/443), so it matches what a normal request would send.
    default_port = 443 if original.scheme == "https" else 80
    port = original.port
    explicit_port = port is not None and port != default_port
    host_header = f"{host_label}:{port}" if explicit_port else host_label
    headers = {"Host": host_header}
    extensions: dict[str, Any] = {"sni_hostname": host} if original.scheme == "https" else {}
    return original.copy_with(host=pinned_ip), headers, extensions
