"""TCP egress guard for USER-supplied DB hosts (ORAA-4 §21 domain layer — pure, no I/O except DNS).

A SQL ingest resolves a ``connection_string`` from the broker and then opens a RAW TCP connection to
its ``host:port`` (asyncpg / aiomysql). The existing HRS + CRS egress guards are HTTP(S)-URL guards
(they require an http(s) scheme via ``urlsplit``/``urlparse``) — a bare DB DSN (``postgres://…`` /
``mysql://…`` / a host:port) is NOT an http URL, so neither covers this raw DB TCP connection. This
module is the DECIDED "Option B" structural TCP check for that gap (Reza sign-off, #307).

The structural rules mirror the CRS MCP guard (blocked hostnames + suffixes + single-label-host
block + literal-IP classification) and add the HRS ``allow_private`` single-tenant mode, applied to
a ``host`` (no scheme) rather than a URL. BLOCK by default:

  * link-local 169.254.0.0/16 (incl. the cloud-metadata 169.254.169.254) and IPv6 link-local —
    ALWAYS blocked, in either mode: no legitimate DB here, only SSRF into instance metadata;
  * loopback (127.0.0.0/8, ::1), RFC-1918 (10/8, 172.16/12, 192.168/16), IPv6 ULA (fc00::/7);
  * the literal blocked hostnames (``localhost``, ``metadata``, ``metadata.google.internal``);
  * the internal suffixes ``.internal`` / ``.local`` / ``.localhost`` / ``.cluster.local``;
  * a single-label / bare-container hostname (``postgres``, ``db``) — an internal container name,
    not a real external server (which is an FQDN or a literal IP).

ALLOW a public host or public literal IP. The hostname is RESOLVED and EVERY resolved IP is
re-checked (a public name pointing inward is blocked). Fail-CLOSED: an unresolvable / ambiguous /
unparseable host is rejected.

In ``allow_private`` mode (single-tenant / dev — the flag mirrors the HRS egress ``allow_private``)
the loopback / private / ULA / blocked-hostname / suffix / single-label rules are RELAXED so a user
can ingest from a local or internal DB (``192.168.x``, ``localhost``, a bare ``postgres``); the
link-local / cloud-metadata range stays blocked in EITHER mode.

DNS-rebinding TOCTOU: this is a resolve-time check — a hostname that resolves to a public IP here
could resolve to a private IP at connect time. The fuller fix pins the resolved IP into the outbound
connection. :func:`validate_db_host` therefore RETURNS the validated resolved IP so the caller can
connect to that pinned address (host=<ip>) rather than re-resolving the name (the SQL connector does
exactly this — see ``domain/connectors/sql_connector.py``). This 3rd egress guard (HRS, CRS, now KGS
TCP) is a deliberate MIRROR, not a shared extraction: the HRS/CRS guards are http(s)-URL guards with
divergent rule sets, so a clean cross-service ``packages/`` extraction would have to reshape both —
too invasive for this slice. FLAGGED for a follow-up consolidation into a shared ``packages/`` net
util (see the PR body / #294).
"""

from __future__ import annotations

import ipaddress
import socket

_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")
_LINK_LOCAL_V6 = ipaddress.ip_network("fe80::/10")

_BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata", "metadata.google.internal"})
_BLOCKED_SUFFIXES = (".internal", ".local", ".localhost", ".cluster.local")


class EgressBlockedError(Exception):
    """A user-supplied DB host is blocked by the TCP egress guard (a clear 4xx upstream)."""


def _normalize(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    """An IPv4-mapped IPv6 (``::ffff:a.b.c.d``) routes to the IPv4 address — return that, so the
    link-local / private rules cannot be bypassed by mapping a blocked IPv4 into IPv6."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ipaddress.ip_address(ip.ipv4_mapped)
    return ip


def _is_link_local(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv4Address):
        return ip in _LINK_LOCAL_V4
    return ip in _LINK_LOCAL_V6 or ip.is_link_local


def _is_private(ip: ipaddress._BaseAddress) -> bool:
    """Loopback (127/8, ::1), RFC-1918 (10/8, 172.16/12, 192.168/16), IPv6 ULA (fc00::/7), plus the
    reserved / multicast / unspecified ranges — never a valid external DB target."""
    return ip.is_loopback or ip.is_private or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def _resolve_ips(host: str) -> list[ipaddress._BaseAddress]:
    """Resolve a host to its IP(s). Fail-closed: an unresolvable host raises (never silently
    treated as safe). A bare IP literal is handled by the caller before this is reached."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise EgressBlockedError(f"cannot resolve DB host {host!r}: {exc}") from exc
    addrs: list[ipaddress._BaseAddress] = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addrs:
        raise EgressBlockedError(f"DB host {host!r} resolved to no usable address")
    return addrs


def validate_db_host(host: str, *, allow_private: bool) -> str:
    """Validate a DB ``host`` before a raw TCP connect; return the pinned resolved IP (as a string).

    Args:
        host: the DB host (a hostname OR a literal IP — NO scheme). Empty / blank is rejected.
        allow_private: when True (single-tenant / dev — mirrors HRS egress ``allow_private``),
            loopback / private / ULA / blocked-name / suffix / single-label hosts are ALLOWED so a
            user can ingest from a local or internal DB; the link-local / cloud-metadata range stays
            blocked. When False (multi-tenant default), all of those are blocked.

    Returns:
        The validated IP address string the caller should connect to (DNS-rebinding TOCTOU: the
        caller pins THIS address rather than re-resolving the hostname).

    Raises:
        EgressBlockedError: the host is unsafe, unresolvable, or ambiguous (fail-closed).
    """
    host = (host or "").strip().lower()
    if not host:
        raise EgressBlockedError("DB host is empty")
    # Strip an IPv6 literal's brackets (``[::1]`` → ``::1``).
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]

    # A literal IP host — classify it directly (no DNS, no name rules).
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        literal = _normalize(literal)
        if _is_link_local(literal):
            raise EgressBlockedError(
                f"DB host {host!r} is a link-local/metadata address ({literal}) (always blocked)"
            )
        if not allow_private and _is_private(literal):
            raise EgressBlockedError(
                f"DB host {host!r} is a private/loopback address ({literal}) "
                "(blocked in this deployment)"
            )
        return str(literal)

    # A hostname. The name-based rules (blocked names, internal suffixes, single-label) only apply
    # in multi-tenant mode; single-tenant relaxes them so a bare `postgres`/`localhost` works.
    if not allow_private:
        if host in _BLOCKED_HOSTNAMES:
            raise EgressBlockedError(f"DB host {host!r} is not allowed in this deployment")
        if any(host.endswith(suffix) for suffix in _BLOCKED_SUFFIXES):
            raise EgressBlockedError(
                f"DB host {host!r} targets an internal suffix (blocked in this deployment)"
            )
        if "." not in host:
            raise EgressBlockedError(
                f"DB host {host!r} is a single-label/bare-container name "
                "(blocked in this deployment; use an FQDN or a public IP)"
            )

    # Resolve and re-check EVERY resolved IP (a public name pointing inward is blocked). Pin the
    # first safe address for the caller to connect to (mitigates the DNS-rebinding TOCTOU).
    resolved = _resolve_ips(host)
    pinned: str | None = None
    for ip in resolved:
        ip = _normalize(ip)
        if _is_link_local(ip):
            raise EgressBlockedError(
                f"DB host {host!r} resolves to a link-local/metadata address ({ip}) (blocked)"
            )
        if not allow_private and _is_private(ip):
            raise EgressBlockedError(
                f"DB host {host!r} resolves to a private/loopback address ({ip}) "
                "(blocked in this deployment)"
            )
        if pinned is None:
            pinned = str(ip)
    if pinned is None:  # defensive: _resolve_ips guarantees ≥1, but never return an unset pin
        raise EgressBlockedError(f"DB host {host!r} resolved to no usable address")
    return pinned
