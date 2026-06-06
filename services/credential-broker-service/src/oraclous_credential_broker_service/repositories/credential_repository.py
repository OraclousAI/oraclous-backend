"""Credential repository (reshape of legacy ``app/repositories/credential_repository.py``).

Every read and write is scoped by ``organisation_id`` as defense-in-depth: the
authenticated principal already binds the user, and this stops a leaked or
guessed credential id from crossing an organisation boundary (Structured Threat
Catalogue T6, ADR-006). ``organisation_id`` is supplied by the caller from the
authenticated context, never from a request body.
"""

from __future__ import annotations

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


class CredentialRepository:
    def __init__(self, db_url: str) -> None:
        self._engine = create_async_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def create_tables(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create_credential(
        self, cred: CreateCredential, organisation_id: UUID, user_id: UUID
    ) -> UserCredential:
        # user_id is bound from the authenticated principal, never the request body.
        obj = UserCredential(
            organisation_id=organisation_id,
            name=cred.name,
            provider=cred.provider,
            user_id=user_id,
            tool_id=cred.tool_id,
            encrypted_cred=encrypt_secret(cred.credential),
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
                obj.encrypted_cred = encrypt_secret(update.credential)
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
                obj.encrypted_cred = encrypt_secret(credential)
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
