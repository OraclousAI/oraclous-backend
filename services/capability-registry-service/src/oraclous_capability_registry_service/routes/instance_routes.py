"""Tool instance routes (ORAA-4 §21 routes layer).

Thin handlers over the instance manager + validation service. The org and owning user come from the
authenticated principal (ORG001), never the request body. ``InstanceNotFoundError`` /
``CapabilityNotFoundError`` map to 404, ``InvalidDescriptorError`` to 422 (factory handlers).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from oraclous_capability_registry_service.core.dependencies import (
    InstanceManagerDep,
    OrganisationIdDep,
    PrincipalDep,
    ValidationServiceDep,
)
from oraclous_capability_registry_service.schema.instance_schema import (
    ConfigureCredentials,
    CreateInstance,
    InstanceListResponse,
    InstanceOut,
    ValidationReport,
)

router = APIRouter(prefix="/api/v1/instances", tags=["instances"])


@router.get("", response_model=InstanceListResponse)
async def list_instances(
    organisation_id: OrganisationIdDep, mgr: InstanceManagerDep
) -> InstanceListResponse:
    items = await mgr.list(organisation_id=organisation_id)
    return InstanceListResponse(instances=items, total=len(items))


@router.post("", response_model=InstanceOut, status_code=status.HTTP_201_CREATED)
async def create_instance(
    body: CreateInstance, principal: PrincipalDep, mgr: InstanceManagerDep
) -> InstanceOut:
    return await mgr.create(
        body=body,
        organisation_id=principal.organisation_id,
        user_id=principal.principal_id,
    )


@router.get("/{instance_id}", response_model=InstanceOut)
async def get_instance(
    instance_id: UUID, organisation_id: OrganisationIdDep, mgr: InstanceManagerDep
) -> InstanceOut:
    return await mgr.get(instance_id=instance_id, organisation_id=organisation_id)


@router.post("/{instance_id}/configure-credentials", response_model=InstanceOut)
async def configure_credentials(
    instance_id: UUID,
    body: ConfigureCredentials,
    organisation_id: OrganisationIdDep,
    mgr: InstanceManagerDep,
) -> InstanceOut:
    return await mgr.configure_credentials(
        instance_id=instance_id, body=body, organisation_id=organisation_id
    )


@router.get("/{instance_id}/validate-execution", response_model=ValidationReport)
async def validate_execution(
    instance_id: UUID, organisation_id: OrganisationIdDep, svc: ValidationServiceDep
) -> ValidationReport:
    return await svc.validate_execution_readiness(
        instance_id=instance_id, organisation_id=organisation_id
    )


@router.get("/{instance_id}/health", response_model=ValidationReport)
async def instance_health(
    instance_id: UUID, organisation_id: OrganisationIdDep, svc: ValidationServiceDep
) -> ValidationReport:
    """Alias of validate-execution: the readiness report is the instance's health view."""
    return await svc.validate_execution_readiness(
        instance_id=instance_id, organisation_id=organisation_id
    )
