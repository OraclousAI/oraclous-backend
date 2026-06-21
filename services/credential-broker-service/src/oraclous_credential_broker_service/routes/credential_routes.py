"""Credential CRUD routes (routes layer).

Thin handlers: parse → one CredentialService call → DTO. Both ``organisation_id`` AND ``user_id``
come from the authenticated principal (``OrganisationIdDep`` / ``PrincipalUserIdDep``), never the
request body/query (ORG001) — credentials are personal, so a caller can only see/manage their own.
Responses are metadata only; the decrypted secret is never returned on this user-facing surface
(runtime resolution uses the X-Internal-Key ``/internal/*`` path). ``CredentialNotFoundError`` maps
to 404 (cross-org / cross-user / unknown id are indistinguishable — T1 mask).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from oraclous_credential_broker_service.core.dependencies import (
    CredentialServiceDep,
    OrganisationIdDep,
    PrincipalUserIdDep,
)
from oraclous_credential_broker_service.schema.credential_schema import (
    CreateCredential,
    CredentialOut,
    CredentialsUpdate,
    RequestCredentials,
)

router = APIRouter(prefix="/credentials", tags=["credentials"])


@router.post("/", response_model=CredentialOut, status_code=status.HTTP_201_CREATED)
async def create_credential(
    body: CreateCredential,
    organisation_id: OrganisationIdDep,
    user_id: PrincipalUserIdDep,
    svc: CredentialServiceDep,
) -> CredentialOut:
    # body.user_id is ignored — the owner is bound from the authenticated principal.
    return await svc.create(cred=body, organisation_id=organisation_id, user_id=user_id)


# NOTE: the literal /providers + /available-data-sources paths are declared BEFORE the
# parameterized /{credential_id} so they aren't shadowed by it (Starlette matches in order).
@router.get("/providers")
async def list_providers(
    organisation_id: OrganisationIdDep,
    user_id: PrincipalUserIdDep,
    svc: CredentialServiceDep,
) -> dict:
    """Which providers the authenticated user has connected (org+user scoped from the principal)."""
    return {"providers": await svc.list_providers(user_id=user_id, organisation_id=organisation_id)}


@router.get("/available-data-sources")
async def available_data_sources(
    organisation_id: OrganisationIdDep,
    user_id: PrincipalUserIdDep,
    svc: CredentialServiceDep,
) -> dict:
    """The catalogue data sources unlocked by the authenticated user's connected providers."""
    return {
        "data_sources": await svc.available_data_sources(
            user_id=user_id, organisation_id=organisation_id
        )
    }


@router.get("/{credential_id}", response_model=CredentialOut)
async def get_credential(
    credential_id: UUID,
    organisation_id: OrganisationIdDep,
    user_id: PrincipalUserIdDep,
    svc: CredentialServiceDep,
) -> CredentialOut:
    return await svc.get(
        credential_id=credential_id, organisation_id=organisation_id, user_id=user_id
    )


@router.post("/retrieve/", response_model=list[CredentialOut])
async def list_credentials(
    body: RequestCredentials,
    organisation_id: OrganisationIdDep,
    user_id: PrincipalUserIdDep,
    svc: CredentialServiceDep,
) -> list[CredentialOut]:
    # body.user_id is ignored — the roster is the authenticated user's own credentials.
    return await svc.list(organisation_id=organisation_id, user_id=user_id, tool_id=body.tool_id)


@router.put("/{credential_id}", response_model=CredentialOut)
async def update_credential(
    credential_id: UUID,
    body: CredentialsUpdate,
    organisation_id: OrganisationIdDep,
    user_id: PrincipalUserIdDep,
    svc: CredentialServiceDep,
) -> CredentialOut:
    if credential_id != body.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="credential_id in path does not match id in body",
        )
    return await svc.update(update=body, organisation_id=organisation_id, user_id=user_id)


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    credential_id: UUID,
    organisation_id: OrganisationIdDep,
    user_id: PrincipalUserIdDep,
    svc: CredentialServiceDep,
) -> None:
    await svc.delete(credential_id=credential_id, organisation_id=organisation_id, user_id=user_id)
