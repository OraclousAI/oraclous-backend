"""Graph-ingest connector (domain layer) — the first-party in-loop ingestion tool.

The WRITE twin of the knowledge-retriever connector (#245): where the retriever POSTs a query to the
knowledge-retriever's ``/v1/search/{mode}``, this POSTs ``{graph_id, content, source_type?,
recipe_id?}`` to the knowledge-graph-service's internal ingest endpoint ``/internal/v1/ingest`` and
returns the enqueued job (``{job_id, status}``). It carries NO broker credential — the KGS is
reached over the internal/gateway-trust path (ADR-018): the executor forwards the caller's verified
org identity (``X-Principal-*`` / ``X-Organisation-Id`` gated by ``X-Internal-Key``) built from the
``ExecutionContext``, so the KGS's own org-scoping binds the ingest to the caller's tenant — a
caller can never write into another org's graph. ``dev`` mode forwards a fixed bearer instead (the
KGS resolves it to the shared dev org), so the loop runs key-free in dev/CI.

A missing/invalid ``graph_id`` (a graph not in the caller's org) is left for the KGS's own 4xx to
surface (fail-closed), mapped to a structured failure here — the upstream body is never echoed back
to the caller (no-leak). This lets an agent's OHM declare ``graph-ingest`` and ingest into a graph.
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
_INGEST_PATH = "/internal/v1/ingest"


class GraphIngestConnector(InternalTool):
    """Wraps the knowledge-graph-service's ``/internal/v1/ingest`` as a registry-executable tool."""

    #: injectable httpx transport for tests (None → real network)
    transport: httpx.AsyncBaseTransport | None = None

    def _downstream_headers(self, context: ExecutionContext) -> dict[str, str]:
        """Identity to forward to the KGS (ADR-018), built from the execution context.

        ``dev`` → a fixed bearer (resolved to the shared dev org by the KGS).
        ``gateway``/``jwt`` → the caller's verified principal + org headers gated by the shared
        internal key. The principal type is ``agent`` (the harness loop calls as an agent principal)
        and the org is the context's ``organisation_id`` (ORG001 — it came from the caller's token,
        never the body), so the KGS scopes the ingest to the SAME tenant.
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
        # graph substrate (#524): the per-run bound graph (instance config) is the fallback, so the
        # model never has to invent a UUID; an explicit tool-call graph_id still wins. Either way
        # KGS RLS scopes it to the caller's org (a cross-org graph_id surfaces as the KGS 4xx).
        graph_id = input_data.get("graph_id") or context.configuration.get("graph_id")
        content = input_data.get("content")
        if not isinstance(graph_id, str) or not graph_id.strip():
            return ExecutionResult(
                success=False,
                error_message="'graph_id' is required",
                error_type="INVALID_INPUT",
            )
        if not isinstance(content, str) or not content.strip():
            return ExecutionResult(
                success=False,
                error_message="'content' is required",
                error_type="INVALID_INPUT",
            )
        # The org is NEVER taken from the body (ORG001); it is forwarded from the execution context.
        body: dict[str, Any] = {"graph_id": graph_id, "content": content}
        source_type = input_data.get("source_type")
        if source_type is not None:
            body["source_type"] = source_type
        recipe_id = input_data.get("recipe_id")
        if recipe_id is not None:
            body["recipe_id"] = recipe_id

        settings = get_settings()
        try:
            async with httpx.AsyncClient(
                base_url=settings.KNOWLEDGE_GRAPH_URL.rstrip("/"),
                headers=self._downstream_headers(context),
                timeout=_TIMEOUT_S,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                resp = await client.post(_INGEST_PATH, json=body)
        except httpx.HTTPError:
            return ExecutionResult(
                success=False,
                error_message="the knowledge graph service could not be reached",
                error_type="INGEST_UNREACHABLE",
            )
        return self._result_from_response(resp)

    @staticmethod
    def _result_from_response(resp: httpx.Response) -> ExecutionResult:
        # The KGS accepts an ingest with 202 (the job is enqueued async).
        if resp.status_code != 202:
            # the KGS's own 4xx (e.g. a missing/invalid graph_id, a graph not in the caller's org)
            # surfaces as a structured failure with only the coarse status — the upstream body is
            # never echoed (no-leak).
            return ExecutionResult(
                success=False,
                error_message=f"the knowledge graph service returned {resp.status_code}",
                error_type="INGEST_HTTP_ERROR",
                metadata={"status_code": resp.status_code},
            )
        try:
            job = resp.json()
        except ValueError:
            return ExecutionResult(
                success=False,
                error_message="the knowledge graph service returned a non-JSON body",
                error_type="INGEST_BAD_RESPONSE",
            )
        if not isinstance(job, dict) or "id" not in job:
            return ExecutionResult(
                success=False,
                error_message="the knowledge graph service returned a malformed body",
                error_type="INGEST_BAD_RESPONSE",
            )
        return ExecutionResult(
            success=True,
            data={"job_id": job.get("id"), "status": job.get("status")},
            metadata={"graph_id": job.get("graph_id"), "source_type": job.get("source_type")},
        )
