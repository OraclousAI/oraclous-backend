"""Generic REST connector (domain layer) — curated data sources as a tool (#489).

Fetches a curated :mod:`source_providers` source's endpoint over HTTPS and returns its parsed dict
on the org-scoped Execution row. The ``source_id`` selects a curated provider (never a free URL),
so there is no SSRF-by-arbitrary-URL surface; on top of that, the resolved URL and **every
redirect hop** are screened by the shared :func:`egress_allowed` gate before any request (the same
discipline as :mod:`web_research`). No-leak: an upstream body is never echoed — only a coarse typed
failure. The two shipped sources are keyless public GETs; keyed sources resolve a BYOM api_key via
the broker (ADR-038 D3), a labelled follow-up.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx

from oraclous_capability_registry_service.domain.connectors.source_providers import (
    SourceProviderError,
    get_source_provider,
)
from oraclous_capability_registry_service.domain.egress import egress_allowed
from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)

_HTTP_TIMEOUT_S = 12.0
_OUTER_TIMEOUT_S = 30.0
_MAX_BYTES = 1024 * 1024  # REST payloads are small; cap defends against a hostile large body
_MAX_REDIRECTS = 3
_USER_AGENT = "OraclousRestConnector/1.0"


class GenericRestConnector(InternalTool):
    """Dispatches a curated REST source/endpoint over a SSRF-guarded HTTPS GET (#489)."""

    timeout_s: float = _OUTER_TIMEOUT_S
    #: injectable httpx transport for tests (None → real network)
    transport: httpx.AsyncBaseTransport | None = None

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        source_id = input_data.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            return ExecutionResult(
                success=False, error_message="'source_id' is required", error_type="INVALID_INPUT"
            )
        try:
            provider = get_source_provider(source_id)
        except SourceProviderError as exc:
            return ExecutionResult(success=False, error_message=str(exc), error_type=exc.error_type)
        endpoint = input_data.get("endpoint")
        path = provider.path_for(endpoint) if isinstance(endpoint, str) else None
        if path is None:
            return ExecutionResult(
                success=False,
                error_message=f"'endpoint' must be one of {sorted(provider.endpoints)}",
                error_type="INVALID_INPUT",
            )
        return await self._fetch(provider.base_url.rstrip("/") + path, provider, str(endpoint))

    async def _fetch(self, url: str, provider: Any, endpoint: str) -> ExecutionResult:
        headers = {"User-Agent": _USER_AGENT, "Accept": "application/json, text/plain, */*"}
        current = url
        resp: httpx.Response | None = None
        async with httpx.AsyncClient(
            headers=headers,
            timeout=_HTTP_TIMEOUT_S,
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                # Screen every hop through the shared SSRF egress gate BEFORE requesting it.
                if not await egress_allowed(current):
                    return ExecutionResult(
                        success=False,
                        error_message="the source URL is not an allowed public target",
                        error_type="UNSAFE_URL",
                    )
                try:
                    resp = await client.get(current)
                except httpx.HTTPError:
                    return ExecutionResult(
                        success=False,
                        error_message="the source could not be reached",
                        error_type="FETCH_UNREACHABLE",
                    )
                if resp.is_redirect and resp.headers.get("location"):
                    current = urljoin(current, resp.headers["location"])
                    continue
                break
            else:
                return ExecutionResult(
                    success=False,
                    error_message="too many redirects",
                    error_type="TOO_MANY_REDIRECTS",
                )
        assert resp is not None  # noqa: S101 — the loop sets resp or returns
        if resp.status_code != 200:
            return ExecutionResult(
                success=False,
                error_message=f"the source returned {resp.status_code}",
                error_type="SOURCE_HTTP_ERROR",
                metadata={"status_code": resp.status_code},
            )
        body = resp.text[:_MAX_BYTES]
        try:
            data = provider.parse(endpoint, body)
        except (ValueError, KeyError, IndexError, TypeError):
            return ExecutionResult(
                success=False,
                error_message="the source returned an unexpected response shape",
                error_type="SOURCE_BAD_RESPONSE",
            )
        return ExecutionResult(
            success=True, data=data, metadata={"source_id": provider.name, "endpoint": endpoint}
        )
