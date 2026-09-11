"""Edge-protection pure helpers (domain layer) — no I/O.

Client-IP derivation under an explicit X-Forwarded-For trust boundary, a cheap Content-Length
fast-path check, and the rate-limit decision shape. Pure, testable without Redis or a socket.
"""

from __future__ import annotations

import ipaddress
from typing import NamedTuple

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class RateLimitDecision(NamedTuple):
    allowed: bool
    retry_after: int  # seconds; meaningful only when not allowed


def parse_exempt_networks(raw: str) -> tuple[IPNetwork, ...]:
    """The client networks the edge limiter never throttles (#850), from a comma-separated CIDR
    list.

    Empty (the default) exempts nothing. FAIL-CLOSED on a bad entry: a typo raises at startup rather
    than being skipped, because a silently dropped entry would hide a mis-configured exemption
    behind a limiter that still bites — and a silently WIDENED one would be worse. Only the docker
    e2e overlay (``deploy/docker-compose.e2e.yml``) sets this; a production gateway leaves it empty.
    """
    networks: list[IPNetwork] = []
    for entry in raw.split(","):
        cidr = entry.strip()
        if not cidr:
            continue
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError as exc:
            raise ValueError(f"EDGE_RATE_LIMIT_EXEMPT_CIDRS: {cidr!r} is not a CIDR") from exc
    return tuple(networks)


def is_exempt_client(ip: str, networks: tuple[IPNetwork, ...]) -> bool:
    """True only when ``ip`` parses AND lies inside one of ``networks``. An empty or unparseable
    peer is never exempt (fail-closed: the limiter still keys it)."""
    if not networks or not ip:
        return False
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in network for network in networks)


# The gateway's own liveness + published-contract probes are never rate-limited — throttling them
# would let monitoring/health-check traffic self-DoS the bucket. (The size guard still applies.)
_RATE_LIMIT_EXEMPT = ("/health", "/v1/openapi.json", "/v1/openapi.yaml", "/docs")


def is_rate_limit_exempt(path: str) -> bool:
    # boundary match (like the route table) so /healthz, /docs-evil, /v1/openapi.json.x can't bypass
    return any(path == p or path.startswith(p + "/") for p in _RATE_LIMIT_EXEMPT)


def is_malformed_path(path: str) -> bool:
    """Reject obviously-hostile request paths at the door (R7-SEC S4): a NUL byte, a backslash, or a
    ``..`` traversal segment. The route table is already fail-closed (an unmatched path 404s), but
    rejecting these at the edge with a 400 is defense-in-depth — a ``..`` must never be forwarded
    into an upstream's path, where path semantics could differ."""
    if "\x00" in path or "\\" in path:
        return True
    return any(segment == ".." for segment in path.split("/"))


def client_ip(peer: str | None, xff_header: str | None, *, trusted_proxy_count: int) -> str:
    """The client IP for the rate-limit key, under an explicit XFF trust boundary.

    With ``trusted_proxy_count == 0`` (the default) X-Forwarded-For is IGNORED entirely and the ASGI
    socket peer is used — correct when nothing trusted sits in front. With N>0 we trust ONLY the N
    right-most hops our own infra appended: the client is the (N+1)-th address from the RIGHT of the
    chain. We never trust the left-most value (attacker-set) or the whole chain. A chain shorter
    than the trusted count falls back to the socket peer (the safe key, never a spoofed
    one).
    """
    peer = peer or ""
    if trusted_proxy_count <= 0 or not xff_header:
        return peer
    chain = [hop.strip() for hop in xff_header.split(",") if hop.strip()]
    index = len(chain) - 1 - trusted_proxy_count
    return chain[index] if index >= 0 else peer


def content_length_exceeds(content_length: str | None, cap: int) -> bool:
    """True only when a present + parseable Content-Length POSITIVELY exceeds the cap (the cheap
    fast-path). A missing/unparseable length returns False — the byte counter is authoritative."""
    if content_length is None:
        return False
    try:
        return int(content_length) > cap
    except ValueError:
        return False
