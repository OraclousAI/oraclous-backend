"""Tool execution routes (ORAA-4 §21 routes layer).

Synchronous dispatch of a configured instance + provenance lookup. Org + user come from the
authenticated principal (ORG001). ``ExecutionNotReadyError`` maps to 409 (factory handler).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from oraclous_capability_registry_service.core.dependencies import (
    ExecutionRepositoryDep,
    OrganisationIdDep,
    PrincipalDep,
    ToolExecutionServiceDep,
)
from oraclous_capability_registry_service.domain.errors import CapabilityNotFoundError
from oraclous_capability_registry_service.schema.execution_schema import (
    ExecuteRequest,
    ExecutionOut,
)

router = APIRouter(prefix="/api/v1", tags=["executions"])


@router.post(
    "/instances/{instance_id}/execute",
    response_model=ExecutionOut,
    status_code=status.HTTP_201_CREATED,
)
async def execute_instance(
    instance_id: UUID,
    body: ExecuteRequest,
    principal: PrincipalDep,
    svc: ToolExecutionServiceDep,
) -> ExecutionOut:
    return await svc.execute_sync(
        instance_id=instance_id,
        body=body,
        organisation_id=principal.organisation_id,
        user_id=principal.principal_id,
    )


@router.get("/executions/{execution_id}", response_model=ExecutionOut)
async def get_execution(
    execution_id: UUID, organisation_id: OrganisationIdDep, repo: ExecutionRepositoryDep
) -> ExecutionOut:
    row = await repo.get_by_id(execution_id, organisation_id)
    if row is None:
        raise CapabilityNotFoundError("execution not found")
    return ExecutionOut.model_validate(row)
