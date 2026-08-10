"""Graph CRUD routes (routes layer).

Handlers are thin: parse the request, make ONE service call, map the result (or a domain error) to
HTTP. No business logic, no DB access, no non-BaseModel classes here (§21 routes rules). The org
scope is already bound by the `bind_org_context` dependency chain behind `get_graph_service`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_knowledge_graph_service.core.dependencies import (
    GrantServiceDep,
    GraphServiceDep,
    UserIdDep,
)
from oraclous_knowledge_graph_service.schema.graph_schemas import (
    CreateGraphRequest,
    GraphGrantRequest,
    GraphGrantResponse,
    GraphResponse,
    UpdateGraphRequest,
)
from oraclous_knowledge_graph_service.services.auth_client import AuthServiceUnavailable
from oraclous_knowledge_graph_service.services.grant_service import (
    GranteeNotInOrg,
    GrantUnavailable,
)
from oraclous_knowledge_graph_service.services.graph_service import (
    GraphNotFound,
    ReservedGraphName,
)

router = APIRouter(prefix="/api/v1/graphs", tags=["graphs"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="graph not found")


@router.post("", response_model=GraphResponse, status_code=status.HTTP_201_CREATED)
async def create_graph(
    body: CreateGraphRequest, service: GraphServiceDep, user_id: UserIdDep
) -> GraphResponse:
    try:
        graph = await service.create_graph(
            user_id=user_id, name=body.name, description=body.description
        )
    except ReservedGraphName:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="graph name is reserved for system use",
        ) from None
    return GraphResponse.of(graph, viewer_user_id=user_id)


@router.get("", response_model=list[GraphResponse])
async def list_graphs(service: GraphServiceDep, user_id: UserIdDep) -> list[GraphResponse]:
    """The caller ORGANISATION's workspaces (#736 / ADR-051), each marked with `is_owner`.

    The listing gate now equals the read gate: every graph returned here is one the caller can
    already read through the org-scoped `POST /v1/search/*`, and it is exactly the set ADR-026
    federated search fans out over. `user_id` no longer filters the set — it only decides
    ownership on each row."""
    graphs = await service.list_graphs()
    return [GraphResponse.of(g, viewer_user_id=user_id) for g in graphs]


@router.get("/{graph_id}", response_model=GraphResponse)
async def get_graph(
    graph_id: uuid.UUID, service: GraphServiceDep, user_id: UserIdDep
) -> GraphResponse:
    """One workspace. A pure READ, so it takes the org-scoped read gate (#736): a member who sees
    this graph in the list can open it, instead of the console rendering a row that 404s."""
    try:
        graph = await service.get_readable_graph(graph_id=graph_id)
    except GraphNotFound:
        raise _NOT_FOUND from None
    return GraphResponse.of(graph, viewer_user_id=user_id)


@router.patch("/{graph_id}", response_model=GraphResponse)
async def update_graph(
    graph_id: uuid.UUID, body: UpdateGraphRequest, service: GraphServiceDep, user_id: UserIdDep
) -> GraphResponse:
    try:
        graph = await service.update_graph(
            graph_id=graph_id,
            user_id=user_id,
            name=body.name,
            description=body.description,
            model_credential_id=body.model_credential_id,
        )
    except GraphNotFound:
        raise _NOT_FOUND from None
    return GraphResponse.of(graph, viewer_user_id=user_id)


@router.delete("/{graph_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_graph(graph_id: uuid.UUID, service: GraphServiceDep, user_id: UserIdDep) -> None:
    try:
        await service.delete_graph(graph_id=graph_id, user_id=user_id)
    except GraphNotFound:
        raise _NOT_FOUND from None


@router.post(
    "/{graph_id}/grants",
    response_model=GraphGrantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def grant_graph_read(
    graph_id: uuid.UUID,
    body: GraphGrantRequest,
    service: GrantServiceDep,
    owner_user_id: UserIdDep,
) -> GraphGrantResponse:
    """The graph's OWNER shares a READ on it with another organisation's user (#446 — the ReBAC
    gate, ADR-004). Owner-gated: a graph the caller does not own → 404 (no leak). Records a ReBAC
    relation only — writes no data and widens no read predicate (RLS stays the wall)."""
    try:
        await service.grant_read(
            graph_id=graph_id,
            owner_user_id=owner_user_id,
            grantee_organisation_id=body.grantee_organisation_id,
            grantee_user_id=body.grantee_user_id,
        )
    except GraphNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="graph not found"
        ) from None
    except GranteeNotInOrg:  # #464: the grantee is not a member of the grantee org — reject.
        # Shape the 422 as the FastAPI field-error LIST so the gateway's leak-safe passthrough
        # (`extract_validation_details`) surfaces the machine token: `type` -> the `issue`
        # (GRANTEE_NOT_IN_ORG) and `loc` -> the `field` (grantee_user_id). A dict `detail` is NOT
        # extractable at the edge — it degrades to a generic MALFORMED_REQUEST, losing the reason
        # (proven by the deployed gateway e2e). `msg` is for a KGS-direct caller; the edge drops it.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[
                {
                    "loc": ["body", "grantee_user_id"],
                    "type": "grantee_not_in_org",
                    "msg": "grantee user is not a member of the grantee organisation",
                }
            ],
        ) from None
    except AuthServiceUnavailable:  # #464 fail-closed: never grant a cross-org edge un-validated
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="grantee membership could not be verified",
        ) from None
    except GrantUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="rebac store unavailable"
        ) from None
    return GraphGrantResponse(
        graph_id=graph_id,
        grantee_organisation_id=body.grantee_organisation_id,
        grantee_user_id=body.grantee_user_id,
        level=body.level,
        granted=True,
    )
