"""Published-agent management routes (ORAA-4 §21 routes layer) — member-managed, org-scoped.

``POST /v1/agents`` publishes an agent; ``GET /v1/agents`` lists the org's published agents. Both
require a member (user) credential and are scoped to that member's org. The public GET-by-slug +
invoke surface (integration-key auth) is Slice 4 PR2. Registered before the proxy catch-all.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from oraclous_application_gateway_service.core.dependencies import (
    MemberDep,
    PublishedAgentServiceDep,
)
from oraclous_application_gateway_service.schema.published_agent_schemas import (
    PublishAgentRequest,
    PublishedAgentOut,
)
from oraclous_application_gateway_service.services.published_agent_service import (
    PublishedAgentConflict,
)

router = APIRouter(prefix="/v1/agents", tags=["gateway"])


@router.post("", response_model=PublishedAgentOut, status_code=status.HTTP_201_CREATED)
async def publish_agent(
    body: PublishAgentRequest, member: MemberDep, svc: PublishedAgentServiceDep
) -> PublishedAgentOut:
    try:
        return await svc.publish(
            organisation_id=member.organisation_id,
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
async def list_agents(member: MemberDep, svc: PublishedAgentServiceDep) -> list[PublishedAgentOut]:
    return await svc.list_agents(member.organisation_id)
