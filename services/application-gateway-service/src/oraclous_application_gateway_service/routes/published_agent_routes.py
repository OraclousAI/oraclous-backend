"""Published-agent routes (ORAA-4 §21 routes layer) — a member-managed plane + a key-public plane.

Member (user JWT, org-scoped): ``POST /v1/agents`` publishes; ``GET /v1/agents`` lists the org's
agents; ``GET /v1/agents/{slug}/details`` hydrates a single agent's full management view (so a
console detail page need not fetch the whole list and filter client-side). Public (integration key
bound to the agent): ``GET /v1/agents/{slug}`` returns narrow public metadata; ``POST
/v1/agents/{slug}/invoke`` runs the bound capability on the harness. The binding (the key's bound
slug must equal the path slug) is enforced in ``require_bound_key`` before any upstream call. The
member detail read lives at ``/{slug}/details`` because ``GET /{slug}`` is already the key-public
projection (same path+method can carry only one auth plane). All routes register before the proxy
catch-all.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from oraclous_application_gateway_service.core.dependencies import (
    AdminDep,
    BoundKeyDep,
    InvokeServiceDep,
    MemberDep,
    PaginationDep,
    PublishedAgentRepoDep,
    PublishedAgentServiceDep,
)
from oraclous_application_gateway_service.models.published_agent import PublishedAgent
from oraclous_application_gateway_service.schema.invoke_schemas import (
    InvokeRequest,
    InvokeResponse,
    PublicAgentOut,
)
from oraclous_application_gateway_service.schema.published_agent_schemas import (
    PublishAgentRequest,
    PublishedAgentOut,
)
from oraclous_application_gateway_service.services.invoke_service import (
    AgentNotFound,
    AgentNotRunnable,
    UpstreamInvokeError,
)
from oraclous_application_gateway_service.services.published_agent_service import (
    PublishedAgentConflict,
)

router = APIRouter(prefix="/v1/agents", tags=["gateway"])


@router.post("", response_model=PublishedAgentOut, status_code=status.HTTP_201_CREATED)
async def publish_agent(
    body: PublishAgentRequest, admin: AdminDep, svc: PublishedAgentServiceDep
) -> PublishedAgent:
    try:
        return await svc.publish(
            organisation_id=admin.organisation_id,
            slug=body.slug,
            bound_capability_ref=body.bound_capability_ref,
            display_name=body.display_name,
            description=body.description,
        )
    except PublishedAgentConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a published agent with this slug already exists in the organisation",
        ) from exc


@router.get("", response_model=list[PublishedAgentOut])
async def list_agents(
    member: MemberDep, svc: PublishedAgentServiceDep, page: PaginationDep
) -> list[PublishedAgent]:
    return await svc.list_agents(member.organisation_id, limit=page.limit, offset=page.offset)


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def unpublish_agent(slug: str, admin: AdminDep, svc: PublishedAgentServiceDep) -> None:
    """Take a published agent down (soft tombstone -> status='unpublished'), so the existing
    read/invoke 404 paths take effect. Admin-gated, org-scoped, idempotent; 204 on success, 404 if
    no such slug in the caller's org."""
    row = await svc.unpublish(organisation_id=admin.organisation_id, slug=slug)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such published agent")


@router.get("/{slug}/details", response_model=PublishedAgentOut)
async def get_agent_details(
    slug: str, member: MemberDep, svc: PublishedAgentServiceDep
) -> PublishedAgent:
    # member-plane single-agent read (org-scoped) — the full management view, not the key-public
    # projection. Lets a console detail page hydrate by slug without a list + client-side filter.
    row = await svc.get_agent(organisation_id=member.organisation_id, slug=slug)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such published agent")
    return row


@router.get("/{slug}", response_model=PublicAgentOut)
async def get_published_agent(
    slug: str, key: BoundKeyDep, agents: PublishedAgentRepoDep
) -> PublicAgentOut:
    # the binding (key bound to {slug}) is enforced by BoundKeyDep; resolve in the key's org
    row = await agents.get_by_slug(organisation_id=key.principal.organisation_id, slug=slug)
    if row is None or row.status != "active":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such published agent")
    return PublicAgentOut(slug=row.slug, display_name=row.display_name, description=row.description)


@router.post("/{slug}/invoke", response_model=InvokeResponse)
async def invoke_published_agent(
    slug: str, body: InvokeRequest, key: BoundKeyDep, svc: InvokeServiceDep
) -> InvokeResponse:
    try:
        return await svc.invoke(slug=slug, agent_input=body.input, principal=key.principal)
    except AgentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no such published agent"
        ) from exc
    except AgentNotRunnable as exc:
        # a PERMANENT binding fault (the bound capability is not runnable) -> a non-retryable 422,
        # never a retryable 502 the external caller backs off and re-sends forever (#283). The edge
        # envelope maps an unmapped 4xx to a non-retryable code (MALFORMED_REQUEST), so the caller
        # gets a terminal, actionable result.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="the published agent's bound capability is not runnable",
        ) from exc
    except UpstreamInvokeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="the published agent could not be run",
        ) from exc
