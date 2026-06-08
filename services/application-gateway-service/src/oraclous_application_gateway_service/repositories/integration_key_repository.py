"""Integration-key store (ORAA-4 §21 repositories layer) — the ONLY DB access in the gateway.

A gateway-owned Postgres table (ADR-019). ``get_by_prefix`` is the pre-auth lookup that PRODUCES org
context (a UNIQUE prefix → exactly one row), so it is intentionally not org-filtered; every other
read/write is org-scoped (``WHERE organisation_id == org``) per ADR-006 — the gateway matches the
platform's app-layer tenancy (no RLS today; RLS-ready, deferred to a platform-wide hardening pass).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_application_gateway_service.models.integration_key import IntegrationKey


class IntegrationKeyRepository:
    def __init__(self, db_url: str) -> None:
        self._engine = create_async_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def get_by_prefix(self, key_prefix: str) -> IntegrationKey | None:
        """Resolve a key by its UNIQUE non-secret prefix — the pre-auth lookup that establishes org
        context. Not org-filtered (it is what produces the org); a miss returns None and the caller
        fails closed with a generic 401, so this is never a cross-org enumeration oracle."""
        async with self._session() as session:
            result = await session.execute(
                select(IntegrationKey).where(IntegrationKey.key_prefix == key_prefix)
            )
            return result.scalar_one_or_none()

    async def create(
        self,
        *,
        organisation_id: uuid.UUID,
        key_prefix: str,
        key_hash: str,
        last4: str,
        bound_agent_slug: str | None = None,
        capability_allow_list: list | None = None,
        cors_origins: list | None = None,
        rate_limit: int | None = None,
        rate_window_seconds: int | None = None,
        expires_at: datetime | None = None,
    ) -> IntegrationKey:
        """The store primitive (the public mint/CRUD surface is Slice 4; the §22 seed uses this)."""
        row = IntegrationKey(
            organisation_id=organisation_id,
            key_prefix=key_prefix,
            key_hash=key_hash,
            last4=last4,
            bound_agent_slug=bound_agent_slug,
            capability_allow_list=capability_allow_list,
            cors_origins=cors_origins,
            rate_limit=rate_limit,
            rate_window_seconds=rate_window_seconds,
            expires_at=expires_at,
        )
        async with self._session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row
