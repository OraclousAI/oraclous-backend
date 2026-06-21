"""Reverse-proxy orchestration (services layer).

Resolves the request path to an upstream via the route table (closed allow-list — unknown prefix →
``RouteNotFoundError`` → 404), applies the forward header policy (drop ``host`` + hop-by-hop), and
opens the upstream stream via the repository. Returns the still-open upstream response for the route
to stream back. Connect/timeout failures surface as gateway domain errors (→ 502/504).
"""

from __future__ import annotations

import httpx
from oraclous_governance import Principal

from oraclous_application_gateway_service.domain.errors import RouteNotFoundError
from oraclous_application_gateway_service.domain.route_table import RouteTable
from oraclous_application_gateway_service.repositories.upstream_client import UpstreamClient

# Hop-by-hop headers must not be forwarded end-to-end (RFC 7230 §6.1).
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Response headers the gateway NEVER returns downstream (R7-SEC S1): anti-fingerprinting — never
# leak the upstream server software (the upstreams run uvicorn; the gateway drops its own `server`
# via `--no-server-header`) — plus a reflect-guard so a misbehaving upstream can never echo a
# trusted-identity header back to the client.
_RESPONSE_DENYLIST = frozenset(
    {
        "server",
        "x-powered-by",
        "x-principal-id",
        "x-principal-type",
        "x-principal-org-role",
        "x-organisation-id",
        "x-internal-key",
    }
)


def forward_request_headers(
    raw_headers: list[tuple[bytes, bytes]],
    principal: Principal | None,
    *,
    internal_key: str,
    request_id: str | None = None,
) -> list[tuple[bytes, bytes]]:
    """Headers to send upstream. Drops ``host`` (httpx sets it) + hop-by-hop. When the request is
    authenticated, STRIPS any client-supplied trusted-identity headers (anti-spoof) and injects the
    verified ``X-Principal-Id``/``X-Principal-Type``/``X-Organisation-Id``; the original ``Bearer``
    is kept (upstream defense-in-depth). On public paths (no principal) ``X-Principal-*`` are still
    stripped, but a client ``X-Organisation-Id`` passes through (e.g. multi-org login selection).
    Every forwarded request carries ``X-Internal-Key`` (a client-supplied copy is stripped first) so
    upstreams can prove the request came through the gateway (ADR-018 edge-auth attestation).
    When ``request_id`` is given, a client-supplied ``X-Request-Id`` is STRIPPED first (anti-forge,
    as for the trust headers) and the gateway's server-minted id is forwarded so the correlation id
    survives end to end (WP-6)."""
    strip = set(_HOP_BY_HOP)
    strip.add("host")
    # X-Principal-* + X-Internal-Key are pure trust assertions — never accept them from the client.
    strip.add("x-principal-id")
    strip.add("x-principal-type")
    strip.add(
        "x-principal-org-role"
    )  # the role is trust-asserted too (R7-SEC S2), never client-set
    strip.add("x-internal-key")
    if request_id is not None:
        # The request id is gateway-minted; never accept a client-forged copy (anti-forge).
        strip.add("x-request-id")
    if principal is not None:
        strip.add("x-organisation-id")  # gateway asserts the verified org on authenticated paths
    out = [(key, value) for key, value in raw_headers if key.decode("latin-1").lower() not in strip]
    if principal is not None:
        out.append((b"x-principal-id", str(principal.principal_id).encode()))
        out.append((b"x-principal-type", str(principal.principal_type.value).encode()))
        if principal.organisation_id is not None:
            out.append((b"x-organisation-id", str(principal.organisation_id).encode()))
        if principal.org_role is not None:  # propagate the verified role (upstreams may role-gate)
            out.append((b"x-principal-org-role", principal.org_role.encode()))
    out.append((b"x-internal-key", internal_key.encode()))
    if request_id is not None:
        out.append((b"x-request-id", request_id.encode()))
    return out


def response_headers(raw_headers: list[tuple[bytes, bytes]]) -> list[tuple[str, str]]:
    """Headers to return downstream: drop hop-by-hop + framing (Starlette's ``Response`` sets its
    own content-length from the buffered body) + the upstream ``date`` (uvicorn emits its own) +
    the security denylist (anti-fingerprint + trust-header reflect guard, R7-SEC S1); keep
    content-type and the rest verbatim."""
    out: list[tuple[str, str]] = []
    for key, value in raw_headers:
        name = key.decode("latin-1").lower()
        if name in _HOP_BY_HOP or name in ("content-length", "date") or name in _RESPONSE_DENYLIST:
            continue
        out.append((name, value.decode("latin-1")))
    return out


class ProxyService:
    def __init__(
        self, *, route_table: RouteTable, upstream_client: UpstreamClient, internal_key: str
    ) -> None:
        self._route_table = route_table
        self._client = upstream_client
        self._internal_key = internal_key

    async def open_upstream(
        self,
        *,
        method: str,
        path: str,
        query: str,
        raw_headers: list[tuple[bytes, bytes]],
        body: bytes,
        principal: Principal | None = None,
        request_id: str | None = None,
    ) -> httpx.Response:
        entry = self._route_table.resolve(path)
        if entry is None:
            raise RouteNotFoundError(path)
        return await self._client.open(
            method=method,
            url=entry.upstream_url + path,
            headers=forward_request_headers(
                raw_headers, principal, internal_key=self._internal_key, request_id=request_id
            ),
            params=query or None,
            content=body,
        )
