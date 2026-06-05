"""Credential CRUD routes (ORAA-4 §21 routes layer).

Thin handlers: parse → one CredentialService call → DTO. ``organisation_id`` comes from the
authenticated principal (``OrganisationIdDep``), never the request body (ORG001). The service's
``CredentialNotFoundError`` maps to 404 (cross-org / unknown id are indistinguishable — T1 mask).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from oraclous_credential_broker_service.core.dependencies import (
    CredentialServiceDep,
    OrganisationIdDep,
)
from oraclous_credential_broker_service.schema.credential_schema import (
    CreateCredential,
    CredentialOut,
    CredentialsUpdate,
    RequestCredentials,
    RequestCredentialsResponse,
)

router = APIRouter(prefix="/credentials", tags=["credentials"])


@router.post("/", response_model=CredentialOut, status_code=status.HTTP_201_CREATED)
async def create_credential(
    body: CreateCredential, organisation_id: OrganisationIdDep, svc: CredentialServiceDep
) -> CredentialOut:
    return await svc.create(cred=body, organisation_id=organisation_id)


@router.get("/{credential_id}", response_model=RequestCredentialsResponse)
async def get_credential(
    credential_id: UUID, organisation_id: OrganisationIdDep, svc: CredentialServiceDep
) -> RequestCredentialsResponse:
    return await svc.get(credential_id=credential_id, organisation_id=organisation_id)


@router.post("/retrieve/", response_model=list[RequestCredentialsResponse])
async def list_credentials(
    body: RequestCredentials, organisation_id: OrganisationIdDep, svc: CredentialServiceDep
) -> list[RequestCredentialsResponse]:
    return await svc.list(request=body, organisation_id=organisation_id)


@router.put("/{credential_id}", response_model=CredentialOut)
async def update_credential(
    credential_id: UUID,
    body: CredentialsUpdate,
    organisation_id: OrganisationIdDep,
    svc: CredentialServiceDep,
) -> CredentialOut:
    return await svc.update(update=body, organisation_id=organisation_id)


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    credential_id: UUID, organisation_id: OrganisationIdDep, svc: CredentialServiceDep
) -> None:
    await svc.delete(credential_id=credential_id, organisation_id=organisation_id)
