"""Credential repository (reshape of legacy ``app/repositories/credential_repository.py``).

Every read and write is scoped by ``organisation_id`` as defense-in-depth: the
authenticated principal already binds the user, and this stops a leaked or
guessed credential id from crossing an organisation boundary (Structured Threat
Catalogue T6, ADR-006). ``organisation_id`` is supplied by the caller from the
authenticated context, never from a request body.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_credential_broker_service.core.security import encrypt_secret
from oraclous_credential_broker_service.models.base_model import Base
from oraclous_credential_broker_service.models.credential_model import UserCredential
from oraclous_credential_broker_service.models.enums import CredentialType
from oraclous_credential_broker_service.schema.credential_schema import (
    CreateCredential,
    CredentialsUpdate,
    RequestCredentials,
)

# the org-aware encrypt seam, dependency-INVERTED so the repository never imports the
# services-layer EnvelopeService (ADR-020 / §21 layering): production injects
# ``EnvelopeService.encrypt`` (writes the v2 envelope); the default writes the legacy single-key
# v1 (back-compat for direct/test construction).
EncryptFn = Callable[..., Awaitable[str]]


async def _legacy_encrypt(*, organisation_id: UUID, plaintext: Any) -> str:  # noqa: ARG001
    return encrypt_secret(plaintext)


class CredentialRepository:
    def __init__(self, db_url: str, *, encrypt: EncryptFn | None = None) -> None:
        self._engine = create_async_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)
        self._encrypt: EncryptFn = encrypt or _legacy_encrypt

    async def create_tables(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create_credential(
        self, cred: CreateCredential, organisation_id: UUID, user_id: UUID
    ) -> UserCredential:
        # user_id is bound from the authenticated principal, never the request body.
        encrypted = await self._encrypt(organisation_id=organisation_id, plaintext=cred.credential)
        obj = UserCredential(
            organisation_id=organisation_id,
            name=cred.name,
            provider=cred.provider,
            user_id=user_id,
            tool_id=cred.tool_id,
            encrypted_cred=encrypted,
            cred_type=CredentialType(cred.cred_type),
        )
        async with self._session() as session:
            async with session.begin():
                session.add(obj)
            await session.refresh(obj)
            return obj

    async def get_credential_by_id(
        self, cred_id: UUID, organisation_id: UUID, user_id: UUID | None = None
    ) -> UserCredential | None:
        # user_id filters the user-facing read to the caller's own credential; the trusted runtime
        # resolver (service→service) passes None and scopes by org only.
        async with self._session() as session:
            stmt = select(UserCredential).where(
                UserCredential.id == cred_id,
                UserCredential.organisation_id == organisation_id,
            )
            if user_id is not None:
                stmt = stmt.where(UserCredential.user_id == user_id)
            result = await session.execute(stmt)
            return result.scalars().first()

    async def list_credentials(
        self, request: RequestCredentials, organisation_id: UUID
    ) -> list[UserCredential]:
        async with self._session() as session:
            stmt = select(UserCredential).where(
                UserCredential.organisation_id == organisation_id,
                UserCredential.user_id == request.user_id,
            )
            if request.tool_id is not None:
                stmt = stmt.where(UserCredential.tool_id == request.tool_id)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def update_credential(
        self, update: CredentialsUpdate, organisation_id: UUID, user_id: UUID
    ) -> UserCredential | None:
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(UserCredential).where(
                        UserCredential.id == update.id,
                        UserCredential.organisation_id == organisation_id,
                        UserCredential.user_id == user_id,
                    )
                )
                obj = result.scalars().first()
                if obj is None:
                    return None
                if update.name is not None:
                    obj.name = update.name
                obj.provider = update.provider
                # ownership is immutable here — keep the credential bound to the authenticated user.
                obj.user_id = user_id
                obj.tool_id = update.tool_id
                obj.cred_type = CredentialType(update.cred_type)
                # Only rotate the secret when a new one is supplied; otherwise preserve the stored
                # ciphertext (a name-only rename never re-sends the secret — FE §1.5).
                if update.credential is not None:
                    obj.encrypted_cred = await self._encrypt(
                        organisation_id=organisation_id, plaintext=update.credential
                    )
            return obj

    async def update_encrypted_credential(
        self, cred_id: UUID, organisation_id: UUID, credential: dict
    ) -> bool:
        """Re-encrypt + store a credential's secret in place (used by runtime-token refresh)."""
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(UserCredential).where(
                        UserCredential.id == cred_id,
                        UserCredential.organisation_id == organisation_id,
                    )
                )
                obj = result.scalars().first()
                if obj is None:
                    return False
                obj.encrypted_cred = await self._encrypt(
                    organisation_id=organisation_id, plaintext=credential
                )
            return True

    async def delete_credential(self, cred_id: UUID, organisation_id: UUID, user_id: UUID) -> bool:
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(UserCredential).where(
                        UserCredential.id == cred_id,
                        UserCredential.organisation_id == organisation_id,
                        UserCredential.user_id == user_id,
                    )
                )
                obj = result.scalars().first()
                if obj is None:
                    return False
                await session.delete(obj)
            return True

    async def iter_all_ciphertexts(self) -> list[tuple[UUID, UUID, str]]:
        """Every credential as ``(id, organisation_id, encrypted_cred)`` — the ADR-020 backfill
        re-encrypts v1 → v2 (an operator-only sweep, not an org-scoped read path)."""
        async with self._session() as session:
            result = await session.execute(
                select(
                    UserCredential.id,
                    UserCredential.organisation_id,
                    UserCredential.encrypted_cred,
                )
            )
            return [(r[0], r[1], r[2]) for r in result.all()]

    async def set_encrypted_cred(self, *, cred_id: UUID, encrypted_cred: str) -> None:
        """Overwrite a row's ciphertext in place (the backfill, after a v1→v2 re-encrypt)."""
        async with self._session() as session, session.begin():
            obj = await session.get(UserCredential, cred_id)
            if obj is not None:
                obj.encrypted_cred = encrypted_cred
