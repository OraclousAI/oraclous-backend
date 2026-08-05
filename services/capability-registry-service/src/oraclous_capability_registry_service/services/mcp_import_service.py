"""MCP import/discovery (services layer, R6 MCP-import; #541).

Register an EXTERNAL MCP server: egress-check + PIN the URL (#492), complete the Streamable-HTTP
handshake (initialize → Mcp-Session-Id → notifications/initialized), call ``tools/list`` WITHIN the
session (SSE-framed responses parsed), and store each discovered tool as a ``kind=tool,
spec.type=mcp`` descriptor in ``pending_approval`` — a supply-chain HITL gate: an imported tool is
NOT executable until an org admin approves it (see ``approve``); an admin can decline it (see
``reject`` → terminal ``rejected``). #541: an optional ``credential_id`` is resolved via the broker
into a Bearer carried through the handshake + discovery, so a HOSTED server that 401s an anonymous
``tools/list`` is importable. The raw MCP/transport error is never surfaced (a generic
``McpImportError``). Stdio/local-subprocess transport stays parked (ADR-038 — cloud-first).
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from oraclous_capability_registry_service.domain.connectors.mcp_protocol import (
    McpProtocolError,
    McpSession,
)
from oraclous_capability_registry_service.domain.egress import egress_allowed
from oraclous_capability_registry_service.domain.mcp_descriptor_shape import mcp_capability_name
from oraclous_capability_registry_service.models.capability_descriptor import CapabilityDescriptor
from oraclous_capability_registry_service.models.enums import DescriptorKind
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityRepository,
)
from oraclous_capability_registry_service.services.credential_client import (
    CredentialBrokerPort,
    CredentialResolutionError,
)

_TIMEOUT_S = 30.0
_MAX_TOOLS = 100  # bound a hostile server's tools/list (no runaway descriptor creation)

PENDING = "pending_approval"
ACTIVE = "active"
REJECTED = "rejected"  # terminal: an admin declined an imported tool — never executable


def _operation_for(tool: dict[str, Any], name: str) -> dict[str, Any]:
    """One descriptor operation per discovered MCP tool (#698 D1).

    One imported descriptor is exactly one MCP tool, so the operation name is the tool name and the
    LLM-visible name stays ``<binding>__<operation>``. ``tools/list`` is UNTRUSTED: a schema that is
    not a JSON object is replaced rather than stored, and the description is capped the same way the
    descriptor's own is. A tool that declares no ``inputSchema`` is legal MCP and still gets an
    operation — it degrades to an empty object schema, never to a missing capability, because a
    missing capability is what makes the tool unreachable in the first place.
    """
    schema = tool.get("inputSchema")
    return {
        "name": name,
        "description": str(tool.get("description") or "")[:500],
        "parameters_schema": (
            schema if isinstance(schema, dict) else {"type": "object", "properties": {}}
        ),
    }


class McpImportError(Exception):
    """The external MCP server could not be reached / discovered (generic — no raw detail leaks).

    #715: carries the originating ``McpProtocolError.code`` and the MCP server's own HTTP status
    when there was one. Both were being discarded at the re-raise, which is why a server that
    answered 401 and a host that never answered arrived at the caller identically. The pair is the
    whole of the added signal — a coarse code and a number, never the upstream body.
    """

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class McpEgressBlocked(McpImportError):
    """The requested server URL is not an allowed external target (SSRF guard) — a client error."""


class McpServerRefusedAuth(McpImportError):
    """The MCP server answered, and refused the request for authentication (401/403).

    Not the platform being unavailable, and not the caller's Oraclous session being bad: the admin
    needs to supply a credential for THIS server, or a better one. The reproduction case on #715 —
    ``https://api.githubcopilot.com/mcp/`` answers promptly and refuses an anonymous ``tools/list``.
    """


class McpServerTimeout(McpImportError):
    """The MCP server did not answer in time — worth retrying, unlike an unreachable host."""


class McpCredentialError(McpImportError):
    """The import credential is at fault, so the failure is OURS and not the MCP server's.

    ``reason`` is a constant machine token naming which credential failure it was. It rides out as
    the ``type`` of a field error on ``credential_id``, so the console can say whether the admin
    should pick a different credential or re-create the one they picked.
    """

    reason = "credential_error"


class McpCredentialUnresolvable(McpCredentialError):
    """The broker could not resolve the supplied ``credential_id`` for this org and user."""

    reason = "credential_unresolvable"


class McpCredentialNotApiKey(McpCredentialError):
    """The credential resolved, but carries no ``api_key`` — it cannot become a Bearer."""

    reason = "credential_not_api_key"


_AUTH_REFUSAL_STATUSES = (401, 403)


def _discovery_failure(exc: McpProtocolError) -> McpImportError:
    """#715: classify a protocol failure instead of flattening it.

    The coarse code and the upstream status both survive onto the returned error, so a caller that
    only knows the base type still learns the cause. The upstream MESSAGE is deliberately left
    behind — a hostile server's body may name internal hosts, and ``McpProtocolError``'s contract is
    that none of it crosses the boundary.
    """
    if exc.code == "MCP_TIMEOUT":
        return McpServerTimeout("the MCP server timed out", code=exc.code, status=exc.status)
    if exc.code == "MCP_HTTP_ERROR" and exc.status in _AUTH_REFUSAL_STATUSES:
        return McpServerRefusedAuth(
            "the MCP server refused the request for authentication",
            code=exc.code,
            status=exc.status,
        )
    return McpImportError(
        "the MCP server could not be discovered", code=exc.code, status=exc.status
    )


class McpImportService:
    def __init__(
        self,
        *,
        capabilities: CapabilityRepository,
        broker: CredentialBrokerPort | None = None,  # #541: resolves credential_id → Bearer
        transport: httpx.AsyncBaseTransport | None = None,  # injectable for tests
    ) -> None:
        self._caps = capabilities
        self._broker = broker
        self._transport = transport

    async def import_server(
        self,
        *,
        organisation_id: uuid.UUID,
        server_url: str,
        label: str,
        user_id: uuid.UUID | None = None,
        credential_id: str | None = None,
    ) -> list[CapabilityDescriptor]:
        """Discover the server's tools and register each as a pending_approval mcp descriptor. #541:
        when ``credential_id`` is given it is resolved (via the broker) into a Bearer carried
        through the handshake + discovery; a resolution failure is FAIL-CLOSED (McpImportError — no
        descriptors created), never an anonymous fallback that could import the wrong server."""
        pinned_ip = await egress_allowed(server_url)  # #492: vet + PIN once, dial the IP below
        if pinned_ip is None:
            raise McpEgressBlocked("the MCP server URL is not an allowed external target")
        bearer = await self._resolve_bearer(organisation_id, user_id, credential_id)
        tools = await self._list_tools(server_url, pinned_ip, bearer)
        created: list[CapabilityDescriptor] = []
        for tool in tools[:_MAX_TOOLS]:
            name = tool.get("name") if isinstance(tool, dict) else None
            if not isinstance(name, str) or not name:
                continue
            name = name[:255]  # bound a hostile server's tool name before it lands in the JSONB
            spec: dict[str, Any] = {
                "type": "mcp",
                "server_url": server_url,
                "tool_name": name,
                # #698 D4: the source server, kept out of the name so an admin can still see and
                # regroup an org's tools by where they came from.
                "label": label,
                # #698 D1: the discovered inputSchema IS the tool's contract. Stored as the
                # descriptor's single operation, it becomes one LLM-callable ToolSpec via
                # tool_specs_for; dropped, the descriptor declares no operations, the model is
                # offered no tool at all, and the import succeeds with a dead tool behind it.
                "capabilities": [_operation_for(tool, name)],
            }
            if credential_id:  # #541: the invoke path resolves this Bearer via the broker too
                spec["credential_id"] = credential_id
                # #698 D2: ToolExecutionService only resolves credentials it finds DECLARED here,
                # so recording the id alone left every call anonymous and a hosted server 401'd.
                # A public server declares nothing, or every call to it would start fail-closing.
                spec["credential_requirements"] = [
                    {"type": "api_key", "provider": "mcp", "required": True}
                ]
            descriptor = {
                "kind": "tool",
                "metadata": {
                    # #698 D4: NOT "<label>/<name>". A "/" here makes the descriptor unnameable —
                    # the runtime's ref slug keeps the tail after the last "/" while its policy
                    # check keeps the head before the first one, so no single ref satisfies both.
                    "name": mcp_capability_name(label, name),
                    "description": str(tool.get("description") or "")[:500],
                },
                "spec": spec,
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

    async def _resolve_bearer(
        self, organisation_id: uuid.UUID, user_id: uuid.UUID | None, credential_id: str | None
    ) -> str | None:
        """#541: resolve ``credential_id`` → a Bearer via the broker (auth'd import). FAIL-CLOSED
        — a requested-but-unresolvable credential raises (no anonymous fallback). None when no
        credential was requested (an anonymous import of a public server)."""
        if not credential_id:
            return None
        if self._broker is None or user_id is None:
            raise McpCredentialUnresolvable(
                "an auth'd MCP import requires a configured credential broker"
            )
        try:
            resolved = await self._broker.resolve(
                organisation_id=organisation_id,
                user_id=user_id,
                requirement={"type": "api_key"},
                credential_id=credential_id,
            )
        except CredentialResolutionError as exc:
            raise McpCredentialUnresolvable("the import credential could not be resolved") from exc
        api_key = resolved.payload.get("api_key") if resolved.payload else None
        if not api_key:
            raise McpCredentialNotApiKey("the import credential is not an api_key")
        return str(api_key)

    async def _list_tools(
        self, server_url: str, pinned_ip: str, bearer: str | None
    ) -> list[dict[str, Any]]:
        """#541: handshake the Streamable-HTTP session (initialize → session id) and call
        ``tools/list`` within it, dialing the #492 pinned IP + carrying the optional Bearer."""
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_S, transport=self._transport, follow_redirects=False
            ) as client:
                session = McpSession(
                    client, server_url=server_url, pinned_ip=pinned_ip, bearer=bearer
                )
                await session.initialize()
                result = await session.call("tools/list", {})
        except McpProtocolError as exc:
            raise _discovery_failure(exc) from exc
        tools = result.get("tools")
        return tools if isinstance(tools, list) else []
