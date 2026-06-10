"""Credential use-cases (ORAA-4 §21 services layer).

Orchestrates the org-scoped repository with the encryption seam: the repository encrypts on write
(AES-256-GCM). Credentials are personal, so every user-facing read/write is scoped by BOTH the
authenticated principal's ``organisation_id`` AND ``user_id`` (never from the request body/query) —
a caller can only see/manage their own credentials. The user-facing surface projects metadata only;
the decrypted secret is NEVER returned here — runtime resolution goes through the X-Internal-Key
``/internal/*`` path (``CredentialBrokerService``), not this service.
"""

from __future__ import annotations

from uuid import UUID

from oraclous_credential_broker_service.domain.providers import data_sources_for
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
from oraclous_credential_broker_service.services.envelope_service import EnvelopeService


class CredentialNotFoundError(Exception):
    """Credential does not exist for the caller's (org, user) — maps to HTTP 404 (mask)."""


def _metadata(row: UserCredential) -> CredentialOut:
    return CredentialOut(
        id=row.id,
        name=row.name,
        provider=row.provider,
        user_id=row.user_id,
        tool_id=row.tool_id,
        cred_type=str(row.cred_type.value if row.cred_type else ""),
    )


class CredentialService:
    def __init__(self, *, repository: CredentialRepository, envelope: EnvelopeService) -> None:
        self._repo = repository
        self._envelope = envelope

    async def _with_secret(self, row: UserCredential) -> RequestCredentialsResponse:
        """Decrypt the stored secret (envelope-polymorphic, org-scoped). ONLY for the trusted
        X-Internal-Key runtime resolver — never the user-facing surface."""
        return RequestCredentialsResponse(
            id=row.id,
            name=row.name,
            provider=row.provider,
            user_id=row.user_id,
            tool_id=row.tool_id,
            cred_type=str(row.cred_type.value if row.cred_type else ""),
            credential=await self._envelope.decrypt(
                organisation_id=row.organisation_id, stored=row.encrypted_cred
            ),
        )

    async def create(
        self, *, cred: CreateCredential, organisation_id: UUID, user_id: UUID
    ) -> CredentialOut:
        return _metadata(await self._repo.create_credential(cred, organisation_id, user_id))

    async def get(
        self, *, credential_id: UUID, organisation_id: UUID, user_id: UUID
    ) -> CredentialOut:
        row = await self._repo.get_credential_by_id(credential_id, organisation_id, user_id)
        if row is None:
            raise CredentialNotFoundError("credential not found")
        return _metadata(row)

    async def resolve_decrypted(
        self, *, credential_id: UUID, organisation_id: UUID
    ) -> RequestCredentialsResponse:
        """Decrypted credential by id, ORG-scoped (no user filter). Trusted runtime path ONLY
        (X-Internal-Key) — resolving a non-OAuth secret for tool execution."""
        row = await self._repo.get_credential_by_id(credential_id, organisation_id)
        if row is None:
            raise CredentialNotFoundError("credential not found")
        return await self._with_secret(row)

    async def list(
        self, *, organisation_id: UUID, user_id: UUID, tool_id: UUID | None = None
    ) -> list[CredentialOut]:
        request = RequestCredentials(user_id=user_id, tool_id=tool_id)
        rows = await self._repo.list_credentials(request, organisation_id)
        return [_metadata(r) for r in rows]

    async def update(
        self, *, update: CredentialsUpdate, organisation_id: UUID, user_id: UUID
    ) -> CredentialOut:
        row = await self._repo.update_credential(update, organisation_id, user_id)
        if row is None:
            raise CredentialNotFoundError("credential not found")
        return _metadata(row)

    async def delete(self, *, credential_id: UUID, organisation_id: UUID, user_id: UUID) -> None:
        if not await self._repo.delete_credential(credential_id, organisation_id, user_id):
            raise CredentialNotFoundError("credential not found")

    async def list_providers(self, *, user_id: UUID, organisation_id: UUID) -> list[str]:
        """The distinct providers a user has connected (order-stable), org+user scoped."""
        rows = await self._repo.list_credentials(
            RequestCredentials(user_id=user_id), organisation_id
        )
        out: list[str] = []
        for r in rows:
            if r.provider not in out:
                out.append(r.provider)
        return out

    async def available_data_sources(
        self, *, user_id: UUID, organisation_id: UUID
    ) -> dict[str, dict]:
        """Data sources unlocked by the user's connected providers (org+user scoped)."""
        providers = await self.list_providers(user_id=user_id, organisation_id=organisation_id)
        return {p: data_sources_for(p) for p in providers}
