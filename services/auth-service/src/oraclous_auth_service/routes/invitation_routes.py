"""Invitation routes (ORAA-4 §21 routes layer).

Admin-gated create/list/revoke under an org; public peek + authenticated accept by token
(rate-limited on the token prefix). Thin handlers — InvitationService raises, factory maps to HTTP.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from oraclous_auth_service.core.dependencies import InvitationServiceDep, UserClaimsDep
from oraclous_auth_service.core.rate_limiter import enforce_invitation_token_prefix_rate_limit
from oraclous_auth_service.models.invitation_model import OrgInvitation
from oraclous_auth_service.schema.invitation_schemas import (
    AcceptInvitationRequest,
    AcceptInvitationResponse,
    CreateInvitationRequest,
    CreateInvitationResponse,
    InvitationPeekResponse,
    InvitationResponse,
    PeekInvitationRequest,
)

org_router = APIRouter(prefix="/v1/orgs", tags=["invitations"])
token_router = APIRouter(prefix="/v1/invitations", tags=["invitations"])


def _invitation_response(inv: OrgInvitation) -> InvitationResponse:
    return InvitationResponse(
        id=inv.id,
        organisation_id=inv.organisation_id,
        email=inv.email,
        role=inv.org_role,
        status=inv.status,
    )


@org_router.post(
    "/{org_id}/invitations",
    response_model=CreateInvitationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_invitation(
    org_id: str,
    body: CreateInvitationRequest,
    claims: UserClaimsDep,
    invitations: InvitationServiceDep,
) -> CreateInvitationResponse:
    inv, raw = await invitations.create_invitation(
        org_id=org_id,
        inviter_user_id=claims["sub"],
        email=body.email,
        role=body.role,
        subgraph_grants=body.subgraph_grants,
    )
    return CreateInvitationResponse(
        id=inv.id,
        organisation_id=inv.organisation_id,
        email=inv.email,
        role=inv.org_role,
        status=inv.status,
        token=raw,
    )


@org_router.get("/{org_id}/invitations", response_model=list[InvitationResponse])
async def list_invitations(
    org_id: str, claims: UserClaimsDep, invitations: InvitationServiceDep
) -> list[InvitationResponse]:
    found = await invitations.list_invitations(org_id=org_id, user_id=claims["sub"])
    return [_invitation_response(i) for i in found]


@org_router.delete("/{org_id}/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(
    org_id: str, invitation_id: str, claims: UserClaimsDep, invitations: InvitationServiceDep
) -> None:
    await invitations.revoke_invitation(
        org_id=org_id, invitation_id=invitation_id, user_id=claims["sub"]
    )


@token_router.post(
    "/peek",
    response_model=InvitationPeekResponse,
    dependencies=[Depends(enforce_invitation_token_prefix_rate_limit)],
)
async def peek_invitation(
    body: PeekInvitationRequest, invitations: InvitationServiceDep
) -> InvitationPeekResponse:
    return InvitationPeekResponse(**await invitations.peek(raw_token=body.token))


@token_router.post(
    "/accept",
    response_model=AcceptInvitationResponse,
    dependencies=[Depends(enforce_invitation_token_prefix_rate_limit)],
)
async def accept_invitation(
    body: AcceptInvitationRequest, claims: UserClaimsDep, invitations: InvitationServiceDep
) -> AcceptInvitationResponse:
    result = await invitations.accept(raw_token=body.token, accepter_user_id=claims["sub"])
    return AcceptInvitationResponse(**result)
