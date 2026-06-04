"""Organisation routes (ORAA-4 §21 routes layer).

Thin handlers: parse → one OrgService call → DTO. Org membership/role authorisation lives in the
service; its `OrgNotFoundError` (404) / `OrgForbiddenError` (403) are mapped by `app/factory.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from oraclous_auth_service.core.dependencies import OrgServiceDep, UserClaimsDep
from oraclous_auth_service.models.organisation_model import Organisation
from oraclous_auth_service.schema.org_schemas import (
    CreateOrgRequest,
    OrgResponse,
    UpdateOrgRequest,
)

router = APIRouter(prefix="/v1/orgs", tags=["orgs"])


def _org_response(org: Organisation) -> OrgResponse:
    return OrgResponse(
        id=org.id,
        name=org.name,
        slug=org.slug,
        description=org.description,
        logo_url=org.logo_url,
        owner_user_id=org.owner_user_id,
        status=org.status,
    )


@router.post("", response_model=OrgResponse, status_code=status.HTTP_201_CREATED)
async def create_org(
    body: CreateOrgRequest, claims: UserClaimsDep, orgs: OrgServiceDep
) -> OrgResponse:
    org = await orgs.create_org(name=body.name, owner_user_id=claims["sub"])
    return _org_response(org)


@router.get("", response_model=list[OrgResponse])
async def list_orgs(claims: UserClaimsDep, orgs: OrgServiceDep) -> list[OrgResponse]:
    found = await orgs.list_for_user(user_id=claims["sub"])
    return [_org_response(o) for o in found]


@router.get("/{org_id}", response_model=OrgResponse)
async def get_org(org_id: str, claims: UserClaimsDep, orgs: OrgServiceDep) -> OrgResponse:
    org = await orgs.get_org(org_id=org_id, user_id=claims["sub"])
    return _org_response(org)


@router.patch("/{org_id}", response_model=OrgResponse)
async def update_org(
    org_id: str, body: UpdateOrgRequest, claims: UserClaimsDep, orgs: OrgServiceDep
) -> OrgResponse:
    org = await orgs.update_org(
        org_id=org_id,
        user_id=claims["sub"],
        name=body.name,
        description=body.description,
        logo_url=body.logo_url,
    )
    return _org_response(org)
