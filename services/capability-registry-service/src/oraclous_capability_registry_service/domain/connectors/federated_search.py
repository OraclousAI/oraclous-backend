"""Federated-search connector (domain layer) — cross-graph search as an agent tool.

Issue #330 / ADR-026. The federation twin of the knowledge-retriever connector: a first-party,
org-scoped, credential-free read tool an agent binds in-loop to search ALL the workspaces its
caller can read from one place. It POSTs the knowledge-retriever's ``/v1/federated/search`` (modes
``entity`` / ``semantic`` / ``fulltext`` / ``hybrid``; every hit labeled ``source_graph_id`` /
``source_graph_name``). It carries NO broker credential — the retriever is reached over the
internal/gateway path (ADR-018): the executor forwards the caller's verified org identity
(``X-Principal-*`` / ``X-Organisation-Id`` gated by ``X-Internal-Key``), so the retriever's own
accessible-set enumeration + org-scoping bind the fan-out to the caller's tenant — federation
grants NO new access, in-loop included. ``dev`` mode forwards a fixed bearer instead.

A missing ``query`` is rejected before the network (fail-closed); an upstream 4xx/5xx surfaces as
a structured failure carrying only the coarse status — the upstream body is never echoed back to
the caller (no-leak), exactly like the retriever + find-similar connectors.
"""

from __future__ import annotations

from typing import Any

import httpx

from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)

# The HTTP timeout must sit UNDER the InternalTool hard timeout (timeout_s, default 30s) — at 60s
# it was dead (the wrapper's asyncio.wait_for fired first, masking the real bound). A small margin
# below the hard timeout lets a slow retriever surface as RETRIEVER_UNREACHABLE rather than the
# generic TIMEOUT. A fan-out over many graphs is the slow case this budget is sized for.
_HTTP_TIMEOUT_MARGIN_S = 2.0
_MODES = ("entity", "semantic", "fulltext", "hybrid")


class FederatedSearchConnector(InternalTool):
    """Wraps the knowledge-retriever's ``POST /v1/federated/search`` as a tool."""

    #: injectable httpx transport for tests (None → real network)
    transport: httpx.AsyncBaseTransport | None = None

    def _downstream_headers(self, context: ExecutionContext) -> dict[str, str]:
        """Identity to forward to the retriever (ADR-018), built from the execution context.

        ``dev`` → a fixed bearer (resolved to the shared dev org by the retriever).
        ``gateway``/``jwt`` → the caller's verified principal + org headers gated by the shared
        internal key, so the retriever enumerates + scopes the fan-out to the SAME tenant."""
        settings = get_settings()
        headers = {"Content-Type": "application/json"}
        if settings.AUTH_MODE == "dev":
            headers["Authorization"] = f"Bearer {settings.DEV_BEARER}"
            return headers
        headers["X-Principal-Id"] = str(context.user_id)
        # Forward the REAL principal type the gateway verified (ADR-018), so the retriever scopes
        # the fan-out to the same principal kind — not a hardcoded "agent". Defaults to agent (the
        # harness loop) on paths that don't set it.
        headers["X-Principal-Type"] = context.principal_type
        headers["X-Organisation-Id"] = str(context.organisation_id)
        if settings.INTERNAL_SERVICE_KEY:
            headers["X-Internal-Key"] = settings.INTERNAL_SERVICE_KEY
        return headers

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        query = input_data.get("query")
        if not isinstance(query, str) or not query.strip():
            return ExecutionResult(
                success=False,
                error_message="'query' is required",
                error_type="INVALID_INPUT",
            )
        mode = input_data.get("mode", "hybrid")
        if mode not in _MODES:
            return ExecutionResult(
                success=False,
                error_message=f"'mode' must be one of {', '.join(_MODES)}",
                error_type="INVALID_INPUT",
            )
        body: dict[str, Any] = {"query": query, "mode": mode}
        graph_ids = input_data.get("graph_ids")
        if graph_ids is not None:
            if not isinstance(graph_ids, list) or not all(
                isinstance(g, str) and g.strip() for g in graph_ids
            ):
                return ExecutionResult(
                    success=False,
                    error_message="'graph_ids' must be a list of graph-id strings",
                    error_type="INVALID_INPUT",
                )
            body["graph_ids"] = graph_ids
        if "per_graph_k" in input_data:
            body["per_graph_k"] = input_data["per_graph_k"]
        if "total_k" in input_data:
            body["total_k"] = input_data["total_k"]

        settings = get_settings()
        try:
            async with httpx.AsyncClient(
                base_url=settings.KNOWLEDGE_RETRIEVER_URL.rstrip("/"),
                headers=self._downstream_headers(context),
                timeout=max(1.0, self.timeout_s - _HTTP_TIMEOUT_MARGIN_S),
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                resp = await client.post("/v1/federated/search", json=body)
        except httpx.HTTPError:
            return ExecutionResult(
                success=False,
                error_message="the knowledge retriever could not be reached",
                error_type="RETRIEVER_UNREACHABLE",
            )
        return self._result_from_response(resp)

    @staticmethod
    def _result_from_response(resp: httpx.Response) -> ExecutionResult:
        if resp.status_code != 200:
            # the retriever's own 4xx (e.g. an inaccessible graph_ids subset → 403, a cap breach →
            # 422) surfaces as a structured failure with only the coarse status — the upstream body
            # is never echoed (no-leak).
            return ExecutionResult(
                success=False,
                error_message=f"the knowledge retriever returned {resp.status_code}",
                error_type="RETRIEVER_HTTP_ERROR",
                metadata={"status_code": resp.status_code},
            )
        try:
            payload = resp.json()
        except ValueError:
            return ExecutionResult(
                success=False,
                error_message="the knowledge retriever returned a non-JSON body",
                error_type="RETRIEVER_BAD_RESPONSE",
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            return ExecutionResult(
                success=False,
                error_message="the knowledge retriever returned a malformed body",
                error_type="RETRIEVER_BAD_RESPONSE",
            )
        return ExecutionResult(
            success=True,
            data={"results": payload["results"], "meta": payload.get("meta", {})},
            metadata={"result_count": len(payload["results"])},
        )
