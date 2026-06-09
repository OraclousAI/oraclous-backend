"""MCP server adapter (ORAA-4 §21 services layer) — JSON-RPC ⇄ published agents + the invoke path.

Translates the MCP subset (initialize / tools/list / tools/call) onto the existing surfaces: the
org's published agents (scoped by the connecting integration key's binding) become MCP tools, and a
tools/call routes through the SYNCHRONOUS published-agent invoke (which forwards to the harness with
the ADR-018 trusted headers + projects the coarse public status). No new execution path; the org's
own agents are already governed/provenanced in the harness, so the server direction adds no gate.
"""

from __future__ import annotations

from typing import Any

from oraclous_application_gateway_service.domain.mcp_protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    call_result_from_invoke,
    initialize_result,
    jsonrpc_error,
    jsonrpc_result,
    tool_descriptor,
)
from oraclous_application_gateway_service.repositories.published_agent_repository import (
    PublishedAgentRepository,
)
from oraclous_application_gateway_service.services.integration_key_auth_service import ResolvedKey
from oraclous_application_gateway_service.services.invoke_service import (
    AgentNotFound,
    InvokeService,
    UpstreamInvokeError,
)


class McpService:
    def __init__(self, *, agents: PublishedAgentRepository, invoke: InvokeService) -> None:
        self._agents = agents
        self._invoke = invoke

    async def dispatch(self, message: Any, key: ResolvedKey) -> dict[str, Any] | None:
        """Handle one JSON-RPC message. Returns the response object, or None for a notification
        (which gets no response — the route then replies 202)."""
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
        ):
            rid = message.get("id") if isinstance(message, dict) else None
            return jsonrpc_error(rid, INVALID_REQUEST, "invalid JSON-RPC request")
        method = message["method"]
        request_id = message.get("id")
        if method == "initialize":
            return jsonrpc_result(request_id, initialize_result())
        if "id" not in message:  # a notification (e.g. notifications/initialized) — no response
            return None
        if method == "tools/list":
            return jsonrpc_result(request_id, {"tools": await self._list_tools(key)})
        if method == "tools/call":
            return await self._call_tool(request_id, message.get("params") or {}, key)
        return jsonrpc_error(request_id, METHOD_NOT_FOUND, f"method not found: {method}")

    async def _list_tools(self, key: ResolvedKey) -> list[dict[str, Any]]:
        """The published agents this key may invoke (org-scoped + per-key binding)."""
        org = key.principal.organisation_id
        if key.bound_agent_slug is not None:
            agent = await self._agents.get_by_slug(organisation_id=org, slug=key.bound_agent_slug)
            rows = [agent] if agent is not None and agent.status == "active" else []
        elif key.capability_allow_list is not None:
            allow = set(key.capability_allow_list)
            rows = [
                a
                for a in await self._agents.list_for_org(org)
                if a.status == "active" and a.bound_capability_ref in allow
            ]
        else:  # a key with no binding sees nothing (fail-closed)
            rows = []
        return [
            tool_descriptor(slug=a.slug, display_name=a.display_name, description=a.description)
            for a in rows
        ]

    async def _is_allowed(self, slug: str, key: ResolvedKey) -> bool:
        # resolve + require active on BOTH binding paths so call agrees with list (a tool not in
        # tools/list is never callable) — not just relying on InvokeService re-validating downstream
        if key.bound_agent_slug is not None:
            if slug != key.bound_agent_slug:
                return False
            agent = await self._agents.get_by_slug(
                organisation_id=key.principal.organisation_id, slug=slug
            )
            return agent is not None and agent.status == "active"
        if key.capability_allow_list is not None:
            agent = await self._agents.get_by_slug(
                organisation_id=key.principal.organisation_id, slug=slug
            )
            return (
                agent is not None
                and agent.status == "active"
                and agent.bound_capability_ref in set(key.capability_allow_list)
            )
        return False

    async def _call_tool(self, request_id: Any, params: Any, key: ResolvedKey) -> dict[str, Any]:
        if not isinstance(params, dict):  # a crafted non-object params must not crash to a 500
            return jsonrpc_error(request_id, INVALID_PARAMS, "params must be an object")
        name = params.get("name")
        args = params.get("arguments")
        if not isinstance(name, str) or not isinstance(args, dict):
            return jsonrpc_error(request_id, INVALID_PARAMS, "name + arguments are required")
        agent_input = args.get("input")
        if not isinstance(agent_input, str) or not agent_input:
            return jsonrpc_error(request_id, INVALID_PARAMS, "arguments.input (string) is required")
        # the tool must be in the key's allowed set — uniform "unknown tool" (don't reveal
        # exists-but-forbidden vs not-exist), fail-closed before any upstream call
        if not await self._is_allowed(name, key):
            return jsonrpc_error(request_id, INVALID_PARAMS, f"unknown tool: {name}")
        try:
            resp = await self._invoke.invoke(
                slug=name, agent_input=agent_input, principal=key.principal
            )
        except AgentNotFound:
            return jsonrpc_error(request_id, INVALID_PARAMS, f"unknown tool: {name}")
        except UpstreamInvokeError:
            # the agent could not run (e.g. a stale binding) -> a tool error, never a raw leak
            return jsonrpc_result(
                request_id,
                {
                    "content": [{"type": "text", "text": "The agent run could not be completed."}],
                    "isError": True,
                },
            )
        return jsonrpc_result(
            request_id,
            call_result_from_invoke(
                status=resp.status, output=resp.output, execution_id=str(resp.execution_id)
            ),
        )
