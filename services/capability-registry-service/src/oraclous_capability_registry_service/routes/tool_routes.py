"""Tool registry routes (ORAA-4 §21 routes layer).

A tool is a capability descriptor of ``kind=tool``; these routes are the tool-shaped view over the
unified registry. Registration derives a deterministic id from the descriptor's identity so the same
tool is stable across deployments and re-registration is idempotent. ``organisation_id`` comes from
the authenticated principal (ORG001).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from oraclous_capability_registry_service.core.dependencies import (
    CapabilityRegistryServiceDep,
    OrganisationIdDep,
)
from oraclous_capability_registry_service.schema.capability_schema import (
    CapabilityListResponse,
    CapabilityOut,
    RegisterTool,
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
