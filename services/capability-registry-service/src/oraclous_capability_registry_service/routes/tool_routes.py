"""Tool registry routes (ORAA-4 §21 routes layer).

A tool is a capability descriptor of ``kind=tool``; these routes are the tool-shaped view over the
unified registry. Registration derives a deterministic id from the descriptor's identity so the same
tool is stable across deployments and re-registration is idempotent. ``organisation_id`` comes from
the authenticated principal (ORG001).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from oraclous_capability_registry_service.core.dependencies import (
    AdminDep,
    CapabilityRegistryServiceDep,
    McpImportServiceDep,
    OrganisationIdDep,
)
from oraclous_capability_registry_service.schema.capability_schema import (
    CapabilityListResponse,
    CapabilityOut,
    ImportMcpRequest,
    ImportMcpResponse,
    RegisterTool,
)
from oraclous_capability_registry_service.services.mcp_import_service import (
    McpEgressBlocked,
    McpImportError,
)

router = APIRouter(prefix="/api/v1/tools", tags=["tools"])


@router.get("", response_model=CapabilityListResponse)
async def list_tools(
    organisation_id: OrganisationIdDep,
    svc: CapabilityRegistryServiceDep,
) -> CapabilityListResponse:
    items = await svc.list_tools(organisation_id=organisation_id)
    return CapabilityListResponse(capabilities=items, total=len(items))


@router.post("", response_model=CapabilityOut, status_code=status.HTTP_201_CREATED)
async def register_tool(
    body: RegisterTool,
    organisation_id: OrganisationIdDep,
    svc: CapabilityRegistryServiceDep,
) -> CapabilityOut:
    return await svc.register_tool(body=body, organisation_id=organisation_id)


@router.get("/{tool_id}", response_model=CapabilityOut)
async def get_tool(
    tool_id: UUID,
    organisation_id: OrganisationIdDep,
    svc: CapabilityRegistryServiceDep,
) -> CapabilityOut:
    return await svc.get(capability_id=tool_id, organisation_id=organisation_id)


@router.post("/import-mcp", response_model=ImportMcpResponse, status_code=status.HTTP_201_CREATED)
async def import_mcp_server(
    body: ImportMcpRequest,
    admin: AdminDep,  # importing an external tool source is an org-admin (supply-chain) action
    svc: McpImportServiceDep,
) -> ImportMcpResponse:
    """Discover an external MCP server's tools and register each as a PENDING_APPROVAL mcp tool. The
    URL is SSRF-egress-checked; the tools are not executable until approved."""
    try:
        created = await svc.import_server(
            organisation_id=admin.organisation_id, server_url=body.server_url, label=body.label
        )
    except McpEgressBlocked as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="the MCP server URL is not an allowed external target",
        ) from exc
    except McpImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="could not import from the MCP server"
        ) from exc
    return ImportMcpResponse(imported=[CapabilityOut.model_validate(r) for r in created])


@router.post("/{tool_id}/approve", status_code=status.HTTP_204_NO_CONTENT)
async def approve_tool(
    tool_id: UUID,
    admin: AdminDep,  # the supply-chain HITL decision — an org admin only
    svc: McpImportServiceDep,
) -> None:
    """Approve an imported MCP tool (pending_approval → active, making it executable). Admin-only;
    org-scoped — an unknown / cross-org id is a 404 (mask)."""
    if not await svc.approve(descriptor_id=tool_id, organisation_id=admin.organisation_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such tool")


@router.post("/{tool_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
async def reject_tool(
    tool_id: UUID,
    admin: AdminDep,  # the other half of the supply-chain HITL decision — an org admin only
    svc: McpImportServiceDep,
) -> None:
    """Decline an imported MCP tool (pending_approval → rejected, a terminal non-executable status).
    Admin-only; org-scoped. Only a still-pending tool can be rejected — an unknown / cross-org id,
    or a tool already approved (active), is a 404 (mask). The descriptor is retained as an audit
    record, not deleted."""
    if not await svc.reject(descriptor_id=tool_id, organisation_id=admin.organisation_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such pending tool")
