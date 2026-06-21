"""Capability registry routes (routes layer).

Thin handlers: parse → one CapabilityRegistryService call → DTO. ``organisation_id`` comes from the
authenticated principal (``OrganisationIdDep``), never the request body (ORG001). The service's
``CapabilityNotFoundError`` maps to 404 (cross-org / unknown id are indistinguishable — mask).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from oraclous_capability_registry_service.core.dependencies import (
    CapabilityRegistryServiceDep,
    OrganisationIdDep,
)
from oraclous_capability_registry_service.models.enums import DescriptorKind
from oraclous_capability_registry_service.schema.capability_schema import (
    CapabilityListResponse,
    CapabilityOut,
    CreateCapability,
    MatchCapabilitiesRequest,
    UpdateCapability,
)

router = APIRouter(prefix="/api/v1/capabilities", tags=["capabilities"])


@router.get("", response_model=CapabilityListResponse)
async def list_capabilities(
    organisation_id: OrganisationIdDep,
    svc: CapabilityRegistryServiceDep,
    kind: DescriptorKind | None = None,
) -> CapabilityListResponse:
    items = await svc.list(organisation_id=organisation_id, kind=kind)
    return CapabilityListResponse(capabilities=items, total=len(items))


@router.post("", response_model=CapabilityOut, status_code=status.HTTP_201_CREATED)
async def register_capability(
    body: CreateCapability,
    organisation_id: OrganisationIdDep,
    svc: CapabilityRegistryServiceDep,
) -> CapabilityOut:
    return await svc.create(body=body, organisation_id=organisation_id)


# Literal sub-paths are declared BEFORE /{capability_id} so they aren't shadowed by it.
@router.post("/match", response_model=CapabilityListResponse)
async def match_capabilities(
    body: MatchCapabilitiesRequest,
    organisation_id: OrganisationIdDep,
    svc: CapabilityRegistryServiceDep,
) -> CapabilityListResponse:
    items = await svc.match_capabilities(
        organisation_id=organisation_id, capability_names=body.capabilities
    )
    return CapabilityListResponse(capabilities=items, total=len(items))


@router.get("/{capability_id}", response_model=CapabilityOut)
async def get_capability(
    capability_id: UUID,
    organisation_id: OrganisationIdDep,
    svc: CapabilityRegistryServiceDep,
) -> CapabilityOut:
    return await svc.get(capability_id=capability_id, organisation_id=organisation_id)


@router.put("/{capability_id}", response_model=CapabilityOut)
async def update_capability(
    capability_id: UUID,
    body: UpdateCapability,
    organisation_id: OrganisationIdDep,
    svc: CapabilityRegistryServiceDep,
) -> CapabilityOut:
    return await svc.update(capability_id=capability_id, body=body, organisation_id=organisation_id)


@router.delete("/{capability_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_capability(
    capability_id: UUID,
    organisation_id: OrganisationIdDep,
    svc: CapabilityRegistryServiceDep,
) -> None:
    await svc.delete(capability_id=capability_id, organisation_id=organisation_id)
