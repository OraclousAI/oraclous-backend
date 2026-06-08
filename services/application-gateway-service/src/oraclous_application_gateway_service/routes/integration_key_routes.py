"""Integration-key management routes (ORAA-4 §21 routes layer) — member-managed, org-scoped.

Mint / list / get / rotate / revoke under ``/v1/integration-keys``. The plaintext secret is returned
ONCE (on mint and rotate) and never again. All routes require a member (user) credential and are
scoped to that member's org. Registered before the proxy catch-all.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from oraclous_application_gateway_service.core.dependencies import KeyManagementDep, MemberDep
from oraclous_application_gateway_service.schema.integration_key_schemas import (
    KeyOut,
    MintedKeyResponse,
    MintKeyRequest,
)
from oraclous_application_gateway_service.services.integration_key_management_service import (
    UnknownBoundAgent,
)

router = APIRouter(prefix="/v1/integration-keys", tags=["gateway"])


def _minted(minted, row) -> MintedKeyResponse:  # noqa: ANN001 — MintedKey + ORM row -> response
    return MintedKeyResponse(
        id=row.id,
        key=minted.plaintext,
        key_prefix=row.key_prefix,
        last4=row.last4,
        bound_agent_slug=row.bound_agent_slug,
        capability_allow_list=row.capability_allow_list,
        status=row.status,
    )


@router.post("", response_model=MintedKeyResponse, status_code=status.HTTP_201_CREATED)
async def mint_key(
    body: MintKeyRequest, member: MemberDep, svc: KeyManagementDep
) -> MintedKeyResponse:
    try:
        minted, row = await svc.mint(
            organisation_id=member.organisation_id,
            bound_agent_slug=body.bound_agent_slug,
            capability_allow_list=body.capability_allow_list,
            cors_origins=body.cors_origins,
            rate_limit=body.rate_limit,
            rate_window_seconds=body.rate_window_seconds,
            expires_at=body.expires_at,
        )
    except UnknownBoundAgent as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="bound_agent_slug does not name a published agent in this organisation",
        ) from exc
    return _minted(minted, row)


@router.get("", response_model=list[KeyOut])
async def list_keys(member: MemberDep, svc: KeyManagementDep) -> list[KeyOut]:
    return await svc.list_keys(member.organisation_id)


@router.get("/{key_id}", response_model=KeyOut)
async def get_key(key_id: uuid.UUID, member: MemberDep, svc: KeyManagementDep) -> KeyOut:
    row = await svc.get(key_id=key_id, organisation_id=member.organisation_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such integration key")
    return row


@router.post("/{key_id}/rotate", response_model=MintedKeyResponse)
async def rotate_key(
    key_id: uuid.UUID, member: MemberDep, svc: KeyManagementDep
) -> MintedKeyResponse:
    minted, row = await svc.rotate(key_id=key_id, organisation_id=member.organisation_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such integration key")
    return _minted(minted, row)


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(key_id: uuid.UUID, member: MemberDep, svc: KeyManagementDep) -> None:
    row = await svc.revoke(key_id=key_id, organisation_id=member.organisation_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such integration key")
