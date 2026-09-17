"""Service-to-service routes (routes layer) — the platform-internal plane.

Everything here is gated on ``X-Internal-Key`` (``verify_internal_key``, constant-time, fail-closed
401) and is reachable ONLY by another Oraclous service: the application-gateway's route table
deliberately carries no ``/internal`` prefix (``domain/route_table.py``), so no request from a human
caller — member or admin — can arrive here through the edge. That is the whole point of the plane,
and why the credential-broker and knowledge-graph services put their own trusted writes on it.

``PUT /internal/v1/instances/{id}/configuration`` (#1130) lives here rather than beside the public
instance routes because the document it replaces carries the run's IDENTITY (``producer``,
``graph_id``, ``working_dir``) — the fields ``graph_ingest``'s ``_producer_config`` trusts precisely
because a model cannot supply them. On the public plane any member of the organisation could have
PUT an arbitrary producer onto a shared instance and then dispatched it, fabricating a provenance
record attributed to another member's run (PR #1133 security review, MAJOR). The org still comes
from the authenticated principal, never the body (ORG001), so the internal key widens *who may
call*, never *whose data is reachable*.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status

from oraclous_capability_registry_service.core.dependencies import (
    InstanceManagerDep,
    OrganisationIdDep,
    PrincipalDep,
    ToolExecutionServiceDep,
    verify_internal_key,
)
from oraclous_capability_registry_service.schema.execution_schema import (
    ExecutionOut,
    InternalExecuteRequest,
)
from oraclous_capability_registry_service.schema.instance_schema import (
    InstanceOut,
    UpdateConfiguration,
)

router = APIRouter(
    prefix="/internal/v1",
    tags=["internal"],
    dependencies=[Depends(verify_internal_key)],
)


@router.put("/instances/{instance_id}/configuration", response_model=InstanceOut)
async def update_configuration(
    instance_id: UUID,
    body: UpdateConfiguration,
    organisation_id: OrganisationIdDep,
    mgr: InstanceManagerDep,
) -> InstanceOut:
    """Replace this instance's stored configuration (#1130). PUT, because it is a full replace."""
    return await mgr.update_configuration(
        instance_id=instance_id, body=body, organisation_id=organisation_id
    )


@router.post(
    "/instances/{instance_id}/execute",
    response_model=ExecutionOut,
    status_code=status.HTTP_201_CREATED,
)
async def execute_instance(
    instance_id: UUID,
    body: InternalExecuteRequest,
    principal: PrincipalDep,
    svc: ToolExecutionServiceDep,
) -> ExecutionOut:
    """Dispatch, carrying the identity of the run making the call (#1130).

    The twin of the member-facing ``POST /api/v1/instances/{id}/execute``, which stays exactly as
    it is. The only difference is ``run_context``: this run's producer / graph / working tree,
    which wins over the instance's shared stored configuration for this dispatch. Stating that is
    an identity assertion, so it is only accepted from a caller that proved it is a service. Org
    and user still come from the principal (ORG001) — the body never names a tenant.
    """
    return await svc.execute_sync(
        instance_id=instance_id,
        body=body,
        organisation_id=principal.organisation_id,
        user_id=principal.principal_id,
        principal_type=principal.principal_type.value,
        run_context=body.run_context,
    )
