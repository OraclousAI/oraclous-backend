"""MCP import/discovery (services layer, R6 MCP-import).

Register an EXTERNAL MCP server: egress-check the URL, call its ``tools/list`` over the hand-built
JSON-RPC subset the executor uses, and store each discovered tool as a ``kind=tool, spec.type=mcp``
descriptor in ``pending_approval`` — a supply-chain HITL gate, so an imported tool is NOT
executable until an org admin approves it (see ``approve``) and an admin can decline an untrusted
tool (see ``reject`` → terminal ``rejected``). The raw MCP/transport error is never
surfaced (a generic ``McpImportError``). Auth'd external servers (import under a broker credential)
are a recorded follow-on; this imports no-auth servers.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from oraclous_capability_registry_service.domain.egress import egress_allowed
from oraclous_capability_registry_service.models.capability_descriptor import CapabilityDescriptor
from oraclous_capability_registry_service.models.enums import DescriptorKind
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityRepository,
)

_TIMEOUT_S = 30.0
_MAX_TOOLS = 100  # bound a hostile server's tools/list (no runaway descriptor creation)

PENDING = "pending_approval"
ACTIVE = "active"
REJECTED = "rejected"  # terminal: an admin declined an imported tool — never executable


class McpImportError(Exception):
    """The external MCP server could not be reached / discovered (generic — no raw detail leaks)."""


class McpEgressBlocked(McpImportError):
    """The requested server URL is not an allowed external target (SSRF guard) — a client error."""


class McpImportService:
    def __init__(
        self,
        *,
        capabilities: CapabilityRepository,
        transport: httpx.AsyncBaseTransport | None = None,  # injectable for tests
    ) -> None:
        self._caps = capabilities
        self._transport = transport

    async def import_server(
        self, *, organisation_id: uuid.UUID, server_url: str, label: str
    ) -> list[CapabilityDescriptor]:
        """Discover the server's tools and register each as a pending_approval mcp descriptor."""
        if not await egress_allowed(server_url):
            raise McpEgressBlocked("the MCP server URL is not an allowed external target")
        tools = await self._list_tools(server_url)
        created: list[CapabilityDescriptor] = []
        for tool in tools[:_MAX_TOOLS]:
            name = tool.get("name") if isinstance(tool, dict) else None
            if not isinstance(name, str) or not name:
                continue
            name = name[:255]  # bound a hostile server's tool name before it lands in the JSONB
            descriptor = {
                "kind": "tool",
                "metadata": {
                    "name": f"{label}/{name}",
                    "description": str(tool.get("description") or "")[:500],
                },
                "spec": {"type": "mcp", "server_url": server_url, "tool_name": name},
            }
            row = await self._caps.create(
                organisation_id=organisation_id,
                kind=DescriptorKind.TOOL,
                descriptor=descriptor,
                status=PENDING,
            )
            created.append(row)
        return created

    async def approve(self, *, descriptor_id: uuid.UUID, organisation_id: uuid.UUID) -> bool:
        """The supply-chain HITL decision: flip an imported tool to ``active`` (executable)."""
        return await self._caps.set_status(
            descriptor_id=descriptor_id, organisation_id=organisation_id, status=ACTIVE
        )

    async def reject(self, *, descriptor_id: uuid.UUID, organisation_id: uuid.UUID) -> bool:
        """The other half of the supply-chain HITL gate: decline an imported tool the admin deems
        untrustworthy (``pending_approval`` → ``rejected``, a terminal non-executable status). The
        descriptor is retained (rejected, not deleted) so the decline is an auditable record. Only
        a still-pending tool can be rejected — an unknown / cross-org id, or an already-``active``
        tool, returns False (the route masks it as a 404)."""
        return await self._caps.set_status_if(
            descriptor_id=descriptor_id,
            organisation_id=organisation_id,
            expected=PENDING,
            status=REJECTED,
        )

    async def _list_tools(self, server_url: str) -> list[dict[str, Any]]:
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        headers = {"content-type": "application/json", "accept": "application/json"}
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_S, transport=self._transport, follow_redirects=False
            ) as client:
                resp = await client.post(server_url, json=rpc, headers=headers)
        except httpx.HTTPError as exc:
            raise McpImportError("the MCP server could not be reached") from exc
        if resp.status_code != 200:
            raise McpImportError(f"the MCP server returned {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise McpImportError("the MCP server returned a non-JSON body") from exc
        if not isinstance(body, dict) or not isinstance(body.get("result"), dict):
            raise McpImportError("the MCP server returned a malformed tools/list result")
        tools = body["result"].get("tools")
        return tools if isinstance(tools, list) else []
