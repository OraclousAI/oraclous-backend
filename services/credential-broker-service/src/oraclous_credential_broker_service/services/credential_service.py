"""Credential use-cases (ORAA-4 §21 services layer).

Orchestrates the org-scoped repository with the encryption seam: the repository encrypts on
write (AES-256-GCM); this service DECRYPTS on read (``decrypt_secret``) when a caller retrieves a
credential, and projects metadata-only on create/update (the secret is never echoed back). Each
call carries the ``organisation_id`` from the authenticated principal (ORG001 — never the body).
"""

from __future__ import annotations

from uuid import UUID

from oraclous_credential_broker_service.core.security import decrypt_secret
from oraclous_credential_broker_service.models.credential_model import UserCredential
from oraclous_credential_broker_service.repositories.credential_repository import (
    CredentialRepository,
)
from oraclous_credential_broker_service.schema.credential_schema import (
    CreateCredential,
    CredentialOut,
    CredentialsUpdate,
    RequestCredentials,
    RequestCredentialsResponse,
)


class CredentialNotFoundError(Exception):
    """Credential does not exist in the caller's organisation — maps to HTTP 404 (mask)."""


def _metadata(row: UserCredential) -> CredentialOut:
    return CredentialOut(
        id=row.id,
        name=row.name,
        provider=row.provider,
        user_id=row.user_id,
        tool_id=row.tool_id,
        cred_type=str(row.cred_type.value if row.cred_type else ""),
    )


def _with_secret(row: UserCredential) -> RequestCredentialsResponse:
    return RequestCredentialsResponse(
        id=row.id,
        name=row.name,
        provider=row.provider,
        user_id=row.user_id,
        tool_id=row.tool_id,
        cred_type=str(row.cred_type.value if row.cred_type else ""),
        credential=decrypt_secret(row.encrypted_cred),
    )


class CredentialService:
    def __init__(self, *, repository: CredentialRepository) -> None:
        self._repo = repository

    async def create(self, *, cred: CreateCredential, organisation_id: UUID) -> CredentialOut:
        return _metadata(await self._repo.create_credential(cred, organisation_id))

    async def get(
        self, *, credential_id: UUID, organisation_id: UUID
    ) -> RequestCredentialsResponse:
        row = await self._repo.get_credential_by_id(credential_id, organisation_id)
        if row is None:
            raise CredentialNotFoundError("credential not found")
        return _with_secret(row)

    async def list(
        self, *, request: RequestCredentials, organisation_id: UUID
    ) -> list[RequestCredentialsResponse]:
        rows = await self._repo.list_credentials(request, organisation_id)
        return [_with_secret(r) for r in rows]

    async def update(self, *, update: CredentialsUpdate, organisation_id: UUID) -> CredentialOut:
        row = await self._repo.update_credential(update, organisation_id)
        if row is None:
            raise CredentialNotFoundError("credential not found")
        return _metadata(row)

    async def delete(self, *, credential_id: UUID, organisation_id: UUID) -> None:
        if not await self._repo.delete_credential(credential_id, organisation_id):
            raise CredentialNotFoundError("credential not found")
