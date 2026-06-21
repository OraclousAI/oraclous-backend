"""Harness↔graph binding routes (routes layer; ADR-029 §6 / Contract §G2).

Thin handlers: parse → one BindingService call → DTO. ``organisation_id`` + ``user_id`` come from
the authenticated principal (``OrganisationIdDep`` / ``PrincipalDep``), never the request body
(ORG001). Mounted under ``/api/v1/agent-bindings`` — the new gateway route-table prefix routed to
the registry (ADR-029 §6; ``/api/v1/graphs/*`` stays wholly on knowledge-graph-service). The
endpoints are query-parameterised (not path-parameterised) so the single static prefix reaches all:

  GET    /api/v1/agent-bindings?graph_id=    → [{harness_id, name, kind, summary}]  (live only)
  GET    /api/v1/agent-bindings?harness_id=  → [{graph_id, name}]                   (live only)
  POST   /api/v1/agent-bindings  {harness_id, graph_id}  → 201 / 200 already-bound / 404 not visible
  DELETE /api/v1/agent-bindings?harness_id=&graph_id=    → 204 / 404 not bound

Errors are curated ``{"detail": …}`` (the service's ``CapabilityNotFoundError`` → 404 via the app
exception handler) and never echo an id or an upstream body (the gateway normalises status → the
canonical ``oraclous_errors`` envelope on egress).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status

from oraclous_capability_registry_service.core.dependencies import (
    BindingServiceDep,
    OrganisationIdDep,
    PrincipalDep,
)
from oraclous_capability_registry_service.schema.binding_schema import (
    BoundAgent,
    BoundGraph,
    CreateBinding,
)

router = APIRouter(prefix="/api/v1/agent-bindings", tags=["agent-bindings"])


@router.get("")
async def list_bindings(
    organisation_id: OrganisationIdDep,
    principal: PrincipalDep,
    svc: BindingServiceDep,
    graph_id: UUID | None = None,
    harness_id: UUID | None = None,
) -> list[BoundAgent] | list[BoundGraph]:
    """List bindings by exactly one of ``graph_id`` (agents for a workspace) or ``harness_id``
    (workspaces a harness serves). Supplying neither or both is a 422 (the filter is ambiguous)."""
    if (graph_id is None) == (harness_id is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="provide exactly one of graph_id or harness_id",
        )
    if graph_id is not None:
        return await svc.list_by_graph(
            organisation_id=organisation_id, user_id=principal.principal_id, graph_id=graph_id
        )
    assert harness_id is not None  # noqa: S101 — guaranteed by the xor check above
    return await svc.list_by_harness(
        organisation_id=organisation_id, user_id=principal.principal_id, harness_id=harness_id
    )


@router.post("")
async def create_binding(
    body: CreateBinding,
    organisation_id: OrganisationIdDep,
    principal: PrincipalDep,
    svc: BindingServiceDep,
    response: Response,
) -> dict[str, bool]:
    """Attach an agent to a workspace. 201 created · 200 if already bound (idempotent) · 404 if
    either object is absent / not visible to the caller's org."""
    created = await svc.attach(
        body=body, organisation_id=organisation_id, user_id=principal.principal_id
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return {"created": created}


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_binding(
    harness_id: UUID,
    graph_id: UUID,
    organisation_id: OrganisationIdDep,
    svc: BindingServiceDep,
) -> None:
    """Detach an agent from a workspace. 204 · 404 if not bound."""
    await svc.detach(organisation_id=organisation_id, harness_id=harness_id, graph_id=graph_id)
