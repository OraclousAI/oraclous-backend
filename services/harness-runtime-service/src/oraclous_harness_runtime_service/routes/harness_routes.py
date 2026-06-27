"""Harness routes (routes layer) — parse → ONE service call → HTTP map. No logic.

``POST .../execute`` runs an inline OHM to completion; ``GET .../executions/{id}`` returns a prior
run (org-scoped). A malformed OHM is the caller's fault (422); a registry/dependency failure during
setup is a bad gateway (502).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from oraclous_governance import Principal
from oraclous_ohm.errors import OHMError

from oraclous_harness_runtime_service.core.dependencies import (
    AssignmentRepositoryDep,
    AssignmentServiceDep,
    ExecutionRepositoryDep,
    HarnessServiceDep,
    PrincipalDep,
    SpendServiceDep,
)
from oraclous_harness_runtime_service.schema.harness_schemas import (
    AssignmentListResponse,
    AssignmentOut,
    CompleteAssignmentRequest,
    ExecuteHarnessRequest,
    ExecutionListResponse,
    HarnessExecutionOut,
    ResumeHarnessRequest,
    SpendResponse,
)
from oraclous_harness_runtime_service.services.assignment_service import AssignmentError
from oraclous_harness_runtime_service.services.harness_execution_service import (
    HarnessExecutionError,
    ResumeError,
)

router = APIRouter(prefix="/v1/harnesses", tags=["harnesses"])


def _require_org(principal: Principal) -> uuid.UUID:
    if principal.organisation_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="no organisation scope"
        )
    return principal.organisation_id


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
            capability_ceiling=body.capability_ceiling,
            parent_execution_id=body.parent_execution_id,
            trace_id=body.trace_id,
            workspace_root=body.workspace_root,
            graph_id=body.graph_id,
            team_id=body.team_id,
            precedence_order=body.precedence_order,
            graph_authoritative=body.graph_authoritative,
            max_tokens=body.max_tokens,
            max_tool_calls=body.max_tool_calls,
        )
    except OHMError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except HarnessExecutionError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return HarnessExecutionOut.model_validate(row)


@router.post("/{execution_id}/resume", response_model=HarnessExecutionOut)
async def resume_harness(
    execution_id: uuid.UUID,
    body: ResumeHarnessRequest,
    principal: PrincipalDep,
    service: HarnessServiceDep,
) -> HarnessExecutionOut:
    """Resolve a mid-loop HITL pause: APPROVED resumes the loop (the gated tool runs), DENIED
    terminates the run FAILED. Driven by the execution-engine task board (R5)."""
    try:
        row = await service.resume(
            execution_id=execution_id,
            principal=principal,
            decision=body.decision,
            decision_reason=body.decision_reason,
        )
    except ResumeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except OHMError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except HarnessExecutionError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return HarnessExecutionOut.model_validate(row)


@router.get("/executions", response_model=ExecutionListResponse)
async def list_executions(
    principal: PrincipalDep, executions: ExecutionRepositoryDep
) -> ExecutionListResponse:
    rows = await executions.list_for_org(_require_org(principal))
    out = [HarnessExecutionOut.model_validate(r) for r in rows]
    return ExecutionListResponse(executions=out, total=len(out))


@router.get("/spend", response_model=SpendResponse)
async def get_spend(
    principal: PrincipalDep,
    service: SpendServiceDep,
    since: datetime | None = None,
) -> SpendResponse:
    """An ESTIMATE of the caller org's provider LLM spend (BYOM), priced from a static rate table —
    NOT platform billing. Per-model raw token sums are priced at read time (ADR-009); unpriced
    models (absent from the table) report tokens only. ``since`` (ISO8601) bounds the window.
    Org-scoped."""
    return await service.estimate(_require_org(principal), since=since)


@router.get("/executions/{execution_id}", response_model=HarnessExecutionOut)
async def get_execution(
    execution_id: uuid.UUID, principal: PrincipalDep, executions: ExecutionRepositoryDep
) -> HarnessExecutionOut:
    row = await executions.get(execution_id, _require_org(principal))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="execution not found")
    return HarnessExecutionOut.model_validate(row)


@router.get("/assignments", response_model=AssignmentListResponse)
async def list_assignments(
    principal: PrincipalDep, assignments: AssignmentRepositoryDep
) -> AssignmentListResponse:
    """The human task board: pending human-actor assignments for the caller's organisation."""
    rows = await assignments.list_for_org(_require_org(principal), status="PENDING")
    out = [AssignmentOut.model_validate(r) for r in rows]
    return AssignmentListResponse(assignments=out, total=len(out))


@router.post("/assignments/{assignment_id}/claim", response_model=AssignmentOut)
async def claim_assignment(
    assignment_id: uuid.UUID, principal: PrincipalDep, service: AssignmentServiceDep
) -> AssignmentOut:
    """A human takes a PENDING task (→ CLAIMED). Driven by the execution-engine task board (R5)."""
    try:
        row = await service.claim(assignment_id, principal)
    except AssignmentError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return AssignmentOut.model_validate(row)


@router.post("/assignments/{assignment_id}/complete", response_model=AssignmentOut)
async def complete_assignment(
    assignment_id: uuid.UUID,
    body: CompleteAssignmentRequest,
    principal: PrincipalDep,
    service: AssignmentServiceDep,
) -> AssignmentOut:
    """The human submits their output (→ COMPLETED); the parked run flips ESCALATED → SUCCEEDED."""
    try:
        row = await service.complete(assignment_id, principal, body.output)
    except AssignmentError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return AssignmentOut.model_validate(row)
