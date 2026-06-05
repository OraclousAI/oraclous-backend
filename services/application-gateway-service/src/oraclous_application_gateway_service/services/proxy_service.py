"""Reverse-proxy orchestration (ORAA-4 §21 services layer).

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


def forward_request_headers(
    raw_headers: list[tuple[bytes, bytes]], principal: Principal | None
) -> list[tuple[bytes, bytes]]:
    """Headers to send upstream. Drops ``host`` (httpx sets it) + hop-by-hop. When the request is
    authenticated, STRIPS any client-supplied trusted-identity headers (anti-spoof) and injects the
    verified ``X-Principal-Id``/``X-Principal-Type``/``X-Organisation-Id``; the original ``Bearer``
    is kept (upstream defense-in-depth). On public paths (no principal) ``X-Principal-*`` are still
    stripped, but a client ``X-Organisation-Id`` passes through (e.g. multi-org login selection)."""
    strip = set(_HOP_BY_HOP)
    strip.add("host")
    # X-Principal-* are pure trust assertions — never accept them from the client, ever.
    strip.add("x-principal-id")
    strip.add("x-principal-type")
    if principal is not None:
        strip.add("x-organisation-id")  # gateway asserts the verified org on authenticated paths
    out = [(key, value) for key, value in raw_headers if key.decode("latin-1").lower() not in strip]
    if principal is not None:
        out.append((b"x-principal-id", str(principal.principal_id).encode()))
        out.append((b"x-principal-type", str(principal.principal_type.value).encode()))
        if principal.organisation_id is not None:
            out.append((b"x-organisation-id", str(principal.organisation_id).encode()))
    return out


def response_headers(raw_headers: list[tuple[bytes, bytes]]) -> list[tuple[str, str]]:
    """Headers to return downstream: drop hop-by-hop + framing headers (the StreamingResponse
    sets its own transfer framing); keep content-type and the rest verbatim."""
    out: list[tuple[str, str]] = []
    for key, value in raw_headers:
        name = key.decode("latin-1").lower()
        if name in _HOP_BY_HOP or name == "content-length":
            continue
        out.append((name, value.decode("latin-1")))
    return out


class ProxyService:
    def __init__(self, *, route_table: RouteTable, upstream_client: UpstreamClient) -> None:
        self._route_table = route_table
        self._client = upstream_client

    async def open_upstream(
        self,
        *,
        method: str,
        path: str,
        query: str,
        raw_headers: list[tuple[bytes, bytes]],
        body: bytes,
        principal: Principal | None = None,
    ) -> httpx.Response:
        entry = self._route_table.resolve(path)
        if entry is None:
            raise RouteNotFoundError(path)
        return await self._client.open(
            method=method,
            url=entry.upstream_url + path,
            headers=forward_request_headers(raw_headers, principal),
            params=query or None,
            content=body,
        )
