"""Knowledge-retriever connector (ORAA-4 §21 domain layer) — the first-party in-loop retrieval tool.

Unlike the SaaS readers, this connector targets a SIBLING internal service (knowledge-retriever),
not a third party: it POSTs the org's ``{query, graph_id, top_k}`` to ``/v1/search/{mode}`` and
returns the ``NodeResult`` hits. It carries NO broker credential — the retriever is reached over the
internal/gateway-trust path (ADR-018): the executor forwards the caller's verified org identity
(``X-Principal-*`` / ``X-Organisation-Id`` gated by ``X-Internal-Key``) built from the
``ExecutionContext``, so the retriever's own org-scoping binds the search to the caller's tenant — a
caller can never read another org's graph. ``dev`` mode forwards a fixed bearer instead (the
retriever resolves it to the shared dev org), so the loop runs key-free in dev/CI.

``mode`` selects the retriever endpoint (``semantic`` default | ``fulltext`` | ``hybrid``). A
missing or invalid ``graph_id`` is left for the retriever's own 4xx to surface (fail-closed), mapped
to a structured failure here — the upstream body is never echoed back to the caller (no-leak).
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
_MODES = frozenset({"semantic", "fulltext", "hybrid"})
_DEFAULT_MODE = "semantic"
_DEFAULT_TOP_K = 10


class KnowledgeRetrieverConnector(InternalTool):
    """Wraps the knowledge-retriever's ``/v1/search/*`` as a registry-executable tool."""

    #: injectable httpx transport for tests (None → real network)
    transport: httpx.AsyncBaseTransport | None = None

    def _downstream_headers(self, context: ExecutionContext) -> dict[str, str]:
        """Identity to forward to the retriever (ADR-018), built from the execution context.

        ``dev`` → a fixed bearer (resolved to the shared dev org by the retriever).
        ``gateway``/``jwt`` → the caller's verified principal + org headers gated by the shared
        internal key. The org is the context's ``organisation_id`` (ORG001 — it came from the
        caller's token, never the body), so the retriever scopes the search to the SAME tenant.
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
        mode = input_data.get("mode", _DEFAULT_MODE)
        if mode not in _MODES:
            return ExecutionResult(
                success=False,
                error_message=f"'mode' must be one of {sorted(_MODES)}",
                error_type="INVALID_INPUT",
            )
        top_k = input_data.get("top_k", _DEFAULT_TOP_K)

        settings = get_settings()
        body = {"query": query, "graph_id": graph_id, "top_k": top_k}
        try:
            async with httpx.AsyncClient(
                base_url=settings.KNOWLEDGE_RETRIEVER_URL.rstrip("/"),
                headers=self._downstream_headers(context),
                timeout=_TIMEOUT_S,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                resp = await client.post(f"/v1/search/{mode}", json=body)
        except httpx.HTTPError:
            return ExecutionResult(
                success=False,
                error_message="the knowledge retriever could not be reached",
                error_type="RETRIEVER_UNREACHABLE",
            )
        return self._result_from_response(resp, mode)

    @staticmethod
    def _result_from_response(resp: httpx.Response, mode: str) -> ExecutionResult:
        if resp.status_code != 200:
            # the retriever's own 4xx (e.g. a missing/invalid graph_id) surfaces as a structured
            # failure with only the coarse status — the upstream body is never echoed (no-leak).
            return ExecutionResult(
                success=False,
                error_message=f"the knowledge retriever returned {resp.status_code}",
                error_type="RETRIEVER_HTTP_ERROR",
                metadata={"status_code": resp.status_code},
            )
        try:
            hits = resp.json()
        except ValueError:
            return ExecutionResult(
                success=False,
                error_message="the knowledge retriever returned a non-JSON body",
                error_type="RETRIEVER_BAD_RESPONSE",
            )
        if not isinstance(hits, list):
            return ExecutionResult(
                success=False,
                error_message="the knowledge retriever returned a malformed body",
                error_type="RETRIEVER_BAD_RESPONSE",
            )
        return ExecutionResult(
            success=True,
            data={"hits": hits},
            metadata={"mode": mode, "hit_count": len(hits)},
        )
