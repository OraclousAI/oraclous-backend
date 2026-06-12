"""Find-similar connector (ORAA-4 §21 domain layer) — the in-loop "entities similar to X" tool.

Issue #310. The twin of the knowledge-retriever connector: a first-party, org-scoped,
credential-free read tool an agent binds in-loop to ask "what is similar to this node?". It GETs the
knowledge-retriever's ``/v1/graph/{graph_id}/similar/{node_id}`` (which traverses the ``SIMILAR_TO``
edges the KGS similarity pass wrote, ranked by the stamped cosine) and returns the ``NodeResult``
hits. It carries NO broker credential — the retriever is reached over the internal/gateway path
(ADR-018): the executor forwards the caller's verified org identity (``X-Principal-*`` /
``X-Organisation-Id`` gated by ``X-Internal-Key``), so the retriever's own org-scoping binds the
lookup to the caller's tenant — a caller can never read another org's graph. ``dev`` mode forwards a
fixed bearer instead, so the loop runs key-free in dev/CI.

A missing ``graph_id``/``node_id`` is rejected before the network (fail-closed); an upstream 4xx
surfaces as a structured failure carrying only the coarse status — the upstream body is never echoed
back to the caller (no-leak), exactly like the retriever connector.
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
_DEFAULT_TOP_K = 10
_DEFAULT_MIN_SCORE = 0.0


class FindSimilarConnector(InternalTool):
    """Wraps the knowledge-retriever's ``/v1/graph/{graph_id}/similar/{node_id}`` as a tool."""

    #: injectable httpx transport for tests (None → real network)
    transport: httpx.AsyncBaseTransport | None = None

    def _downstream_headers(self, context: ExecutionContext) -> dict[str, str]:
        """Identity to forward to the retriever (ADR-018), built from the execution context.

        ``dev`` → a fixed bearer (resolved to the shared dev org by the retriever).
        ``gateway``/``jwt`` → the caller's verified principal + org headers gated by the shared
        internal key, so the retriever scopes the lookup to the SAME tenant the call came from.
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
        node_id = input_data.get("node_id")
        if not isinstance(graph_id, str) or not graph_id.strip():
            return ExecutionResult(
                success=False,
                error_message="'graph_id' is required",
                error_type="INVALID_INPUT",
            )
        if not isinstance(node_id, str) or not node_id.strip():
            return ExecutionResult(
                success=False,
                error_message="'node_id' is required",
                error_type="INVALID_INPUT",
            )
        top_k = input_data.get("top_k", _DEFAULT_TOP_K)
        min_score = input_data.get("min_score", _DEFAULT_MIN_SCORE)

        settings = get_settings()
        params = {"top_k": top_k, "min_score": min_score}
        try:
            async with httpx.AsyncClient(
                base_url=settings.KNOWLEDGE_RETRIEVER_URL.rstrip("/"),
                headers=self._downstream_headers(context),
                timeout=_TIMEOUT_S,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                resp = await client.get(f"/v1/graph/{graph_id}/similar/{node_id}", params=params)
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
            metadata={"hit_count": len(hits)},
        )
