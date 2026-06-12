"""Recall-memory connector (ORAA-4 §21 domain layer) — the in-loop agent-memory recall tool.

Issue #332 / ADR-027 §6. Mirrors ``core/find-similar@1.0.0`` (#310): a first-party, org-scoped,
credential-free read tool an agent OPTS INTO via its OHM toolset as ``core/recall-memory@1.0.0`` —
there is NO change to the harness's default prompt assembly, so existing runs carry zero risk. It
GETs the knowledge-graph-service's ``/api/v1/graphs/{graph_id}/memories/search`` (the hybrid
fulltext + vector + Ebbinghaus-importance + recency recall) and returns the ranked memories. It
carries NO broker credential — the KGS is reached over the internal/gateway-trust path (ADR-018):
the executor forwards the caller's verified org identity (``X-Principal-*`` / ``X-Organisation-Id``
gated by ``X-Internal-Key``), so the KGS's own org-scoping binds the recall to the caller's tenant
— a caller can never read another org's memories. ``dev`` mode forwards a fixed bearer instead.

A missing ``graph_id``/``query`` is rejected before the network (fail-closed); an upstream 4xx
surfaces as a structured failure carrying only the coarse status — the upstream body is never
echoed back to the caller (no-leak), exactly like the retriever/find-similar connectors.
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

_TIMEOUT_S = 30.0
_DEFAULT_LIMIT = 10
_VALID_TYPES = ("episodic", "semantic", "procedural")
_VALID_SCOPES = ("session", "user", "agent", "team", "organization")


class RecallMemoryConnector(InternalTool):
    """Wraps the KGS's ``/api/v1/graphs/{graph_id}/memories/search`` as a registry tool."""

    #: injectable httpx transport for tests (None → real network)
    transport: httpx.AsyncBaseTransport | None = None

    def _downstream_headers(self, context: ExecutionContext) -> dict[str, str]:
        """Identity to forward to the KGS (ADR-018), built from the execution context.

        ``dev`` → a fixed bearer (resolved to the shared dev org by the KGS).
        ``gateway``/``jwt`` → the caller's verified principal + org headers gated by the shared
        internal key, so the KGS scopes the recall to the SAME tenant the call came from.
        """
        settings = get_settings()
        headers = {"Content-Type": "application/json"}
        if settings.AUTH_MODE == "dev":
            headers["Authorization"] = f"Bearer {settings.DEV_BEARER}"
            return headers
        headers["X-Principal-Id"] = str(context.user_id)
        headers["X-Principal-Type"] = "agent"  # the harness loop calls as an agent principal
        headers["X-Organisation-Id"] = str(context.organisation_id)
        if settings.INTERNAL_SERVICE_KEY:
            headers["X-Internal-Key"] = settings.INTERNAL_SERVICE_KEY
        return headers

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        graph_id = input_data.get("graph_id")
        query = input_data.get("query")
        if not isinstance(graph_id, str) or not graph_id.strip():
            return ExecutionResult(
                success=False,
                error_message="'graph_id' is required",
                error_type="INVALID_INPUT",
            )
        if not isinstance(query, str) or not query.strip():
            return ExecutionResult(
                success=False,
                error_message="'query' is required",
                error_type="INVALID_INPUT",
            )
        params: dict[str, Any] = {
            "query": query,
            "limit": input_data.get("limit", _DEFAULT_LIMIT),
        }
        memory_type = input_data.get("type")
        if memory_type is not None:
            if memory_type not in _VALID_TYPES:
                return ExecutionResult(
                    success=False,
                    error_message=f"'type' must be one of {list(_VALID_TYPES)}",
                    error_type="INVALID_INPUT",
                )
            params["type"] = memory_type
        scope = input_data.get("scope")
        if scope is not None:
            if scope not in _VALID_SCOPES:
                return ExecutionResult(
                    success=False,
                    error_message=f"'scope' must be one of {list(_VALID_SCOPES)}",
                    error_type="INVALID_INPUT",
                )
            params["scope"] = scope

        settings = get_settings()
        try:
            async with httpx.AsyncClient(
                base_url=settings.KNOWLEDGE_GRAPH_URL.rstrip("/"),
                headers=self._downstream_headers(context),
                timeout=_TIMEOUT_S,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                resp = await client.get(f"/api/v1/graphs/{graph_id}/memories/search", params=params)
        except httpx.HTTPError:
            return ExecutionResult(
                success=False,
                error_message="the knowledge graph service could not be reached",
                error_type="KGS_UNREACHABLE",
            )
        return self._result_from_response(resp)

    @staticmethod
    def _result_from_response(resp: httpx.Response) -> ExecutionResult:
        if resp.status_code != 200:
            # the KGS's own 4xx (e.g. a missing/invalid graph_id) surfaces as a structured failure
            # with only the coarse status — the upstream body is never echoed (no-leak).
            return ExecutionResult(
                success=False,
                error_message=f"the knowledge graph service returned {resp.status_code}",
                error_type="KGS_HTTP_ERROR",
                metadata={"status_code": resp.status_code},
            )
        try:
            body = resp.json()
        except ValueError:
            return ExecutionResult(
                success=False,
                error_message="the knowledge graph service returned a non-JSON body",
                error_type="KGS_BAD_RESPONSE",
            )
        memories = body.get("memories") if isinstance(body, dict) else None
        if not isinstance(memories, list):
            return ExecutionResult(
                success=False,
                error_message="the knowledge graph service returned a malformed body",
                error_type="KGS_BAD_RESPONSE",
            )
        return ExecutionResult(
            success=True,
            data={"memories": memories, "total": body.get("total", len(memories))},
            metadata={"memory_count": len(memories)},
        )
