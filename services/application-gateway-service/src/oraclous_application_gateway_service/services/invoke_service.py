"""Published-agent invoke (ORAA-4 §21 services layer) — resolve the bound agent, run it, project.

The per-key binding (the key's bound slug must match the invoked agent) is enforced upstream in the
``require_bound_key`` dependency. Here: resolve the slug to a published agent IN THE KEY'S ORG, run
its bound capability on the harness (POST /v1/harnesses/execute with the ADR-018 trusted-identity
headers — the verified SERVICE_ACCOUNT principal + the internal-key attestation; the org is asserted
by the gateway, never the caller), and project the result to the narrow public ``InvokeResponse``
(never the harness internals). A broken binding (stale capability ref -> harness non-2xx) is a 502.
"""

from __future__ import annotations

import json
import uuid

from oraclous_governance import Principal

from oraclous_application_gateway_service.repositories.published_agent_repository import (
    PublishedAgentRepository,
)
from oraclous_application_gateway_service.repositories.upstream_client import UpstreamClient
from oraclous_application_gateway_service.schema.invoke_schemas import InvokeResponse
from oraclous_application_gateway_service.services.proxy_service import forward_request_headers


class AgentNotFound(Exception):
    """No active published agent at this slug in the key's org (-> 404)."""


class UpstreamInvokeError(Exception):
    """The harness could not run the bound capability (e.g. a stale ref) (-> 502)."""


# Collapse the internal HarnessStatus to a COARSE public vocabulary: internal orchestration states
# (ESCALATED/TIMED_OUT/...) and raw failure detail must never reach an external integration-key
# holder. SUCCEEDED -> succeeded; ESCALATED (a human/HITL step is pending) -> pending; everything
# else -> a generic failed.
_PUBLIC_STATUS = {"SUCCEEDED": "succeeded", "ESCALATED": "pending"}


def _public_status(raw: str) -> str:
    return _PUBLIC_STATUS.get(raw.upper(), "failed")


class InvokeService:
    def __init__(
        self,
        *,
        agents: PublishedAgentRepository,
        upstream_client: UpstreamClient,
        harness_base_url: str,
        internal_key: str,
    ) -> None:
        self._agents = agents
        self._upstream = upstream_client
        self._base_url = harness_base_url.rstrip("/")
        self._internal_key = internal_key

    async def invoke(self, *, slug: str, agent_input: str, principal: Principal) -> InvokeResponse:
        agent = await self._agents.get_by_slug(organisation_id=principal.organisation_id, slug=slug)
        if agent is None or agent.status != "active":
            raise AgentNotFound(slug)
        body = json.dumps(
            {"manifest_ref": agent.bound_capability_ref, "input": agent_input}
        ).encode()
        headers = forward_request_headers(
            [(b"content-type", b"application/json")], principal, internal_key=self._internal_key
        )
        resp = await self._upstream.open(
            method="POST",
            url=f"{self._base_url}/v1/harnesses/execute",
            headers=headers,
            params=None,
            content=body,
        )
        try:
            code, raw = resp.status_code, await resp.aread()
        finally:
            await resp.aclose()
        if code not in (200, 201):
            raise UpstreamInvokeError(f"harness returned {code}")
        # a malformed/contract-drifted 2xx body is a 502, never a 500 — keep the whole parse + the
        # id projection inside the guard (a missing/non-UUID id must not crash to INTERNAL_ERROR).
        try:
            data = json.loads(raw)
            execution_id = uuid.UUID(str(data["id"]))
        except (ValueError, TypeError, KeyError) as exc:
            raise UpstreamInvokeError("harness returned a malformed body") from exc
        status = _public_status(str(data.get("status", "")))
        # NEVER forward the raw upstream error_message (un-redacted exception text — provider ids /
        # secrets) to the public plane; the coarse status + a generic detail convey the outcome.
        return InvokeResponse(
            execution_id=execution_id,
            status=status,
            output=data.get("output") if status == "succeeded" else None,
            error="the agent run did not complete successfully" if status == "failed" else None,
        )
