"""Harness routes (ORAA-4 §21 routes layer) — parse → ONE service call → HTTP map. No logic.

``POST .../execute`` runs an inline OHM to completion; ``GET .../executions/{id}`` returns a prior
run (org-scoped). A malformed OHM is the caller's fault (422); a registry/dependency failure during
setup is a bad gateway (502).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_harness_runtime_service.core.dependencies import (
    ExecutionRepositoryDep,
    HarnessServiceDep,
    PrincipalDep,
)
from oraclous_harness_runtime_service.domain.ohm.errors import OHMError
from oraclous_harness_runtime_service.schema.harness_schemas import (
    ExecuteHarnessRequest,
    HarnessExecutionOut,
)
from oraclous_harness_runtime_service.services.harness_execution_service import (
    HarnessExecutionError,
)

router = APIRouter(prefix="/v1/harnesses", tags=["harnesses"])


@router.post("/execute", response_model=HarnessExecutionOut, status_code=status.HTTP_201_CREATED)
async def execute_harness(
    body: ExecuteHarnessRequest, principal: PrincipalDep, service: HarnessServiceDep
) -> HarnessExecutionOut:
    manifest_inline = body.manifest_yaml if body.manifest_yaml is not None else body.manifest
    try:
        row = await service.execute(
            manifest_inline=manifest_inline,
            manifest_ref=body.manifest_ref,
            user_input=body.input,
            principal=principal,
        )
    except OHMError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except HarnessExecutionError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return HarnessExecutionOut.model_validate(row)


@router.get("/executions/{execution_id}", response_model=HarnessExecutionOut)
async def get_execution(
    execution_id: uuid.UUID, principal: PrincipalDep, executions: ExecutionRepositoryDep
) -> HarnessExecutionOut:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="no organisation scope"
        )
    row = await executions.get(execution_id, org_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="execution not found")
    return HarnessExecutionOut.model_validate(row)
