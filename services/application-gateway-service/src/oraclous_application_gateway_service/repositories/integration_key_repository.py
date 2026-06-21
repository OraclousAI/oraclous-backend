"""Integration-key store (repositories layer) — a gateway-owned DB seam.

A gateway-owned Postgres table (ADR-019). ``get_by_prefix`` is the pre-auth lookup that PRODUCES org
context (a UNIQUE prefix → exactly one row), so it is intentionally not org-filtered; every other
read/write is org-scoped (``WHERE organisation_id == org``) per ADR-006, with the Postgres RLS
backstop (ADR-030) behind that app-layer filter.

The RLS backstop forces a TWO-ENGINE split (ADR-030 §3). ``get_by_prefix`` precedes org context, so
it runs on the OWNER engine (``install_guard=False``) which bypasses RLS — else FORCE'd RLS fails it
closed to zero rows and breaks integration-key auth (the HARD RULE). Every ORG-BOUND method runs on
the org-bound ``oraclous_app`` engine (the org-GUC guard installed by ``build_rls_engine``) and
binds the org it received from authenticated context via ``org_scope`` so the begin-guard sets
``app.current_organisation_id`` — without that bind a read returns zero rows and a write raises
42501 (the capability-registry/engine lesson).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from oraclous_substrate import build_rls_engine, org_scope
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_application_gateway_service.domain.pagination import DEFAULT_LIMIT
from oraclous_application_gateway_service.models.integration_key import IntegrationKey


class IntegrationKeyRepository:
    def __init__(self, db_url: str, *, install_guard: bool = True) -> None:
        # install_guard=True (default): the org-bound oraclous_app engine with the org-GUC begin
        # guard, so RLS bites + org_scope binds the GUC per org-bound op. install_guard=False: the
        # OWNER engine for the pre-auth ``get_by_prefix`` producer read (it precedes org context and
        # the owner bypasses RLS, so no guard — mirrors auth's owner-engine credential store).
        self._engine = (
            build_rls_engine(db_url, echo=False)
            if install_guard
            else create_async_engine(db_url, echo=False)
        )
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def get_by_prefix(self, key_prefix: str) -> IntegrationKey | None:
        """Resolve a key by its UNIQUE non-secret prefix — the pre-auth lookup that establishes org
        context. Not org-filtered (it is what produces the org); a miss returns None and the caller
        fails closed with a generic 401, so this is never a cross-org enumeration oracle. Runs on
        the OWNER engine (no bound org — it precedes org context); the owner bypasses RLS so this
        resolves cross-org (ADR-030 §3)."""
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
        """The store primitive (the public mint/CRUD surface is Slice 4; the §22 seed uses this).
        Org-bound: ``org_scope`` binds the GUC so the RLS WITH CHECK admits the INSERT (ADR-030)."""
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
        with org_scope(organisation_id):
            async with self._session() as session:
                session.add(row)
                await session.commit()
                await session.refresh(row)
                return row

    # --- org-scoped CRUD (the member-managed surface, Slice 4). Each binds the org via org_scope so
    # the org-bound engine's GUC guard sets app.current_organisation_id and RLS scopes the op. ---

    async def list_for_org(
        self, organisation_id: uuid.UUID, *, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> list[IntegrationKey]:
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(IntegrationKey)
                    .where(IntegrationKey.organisation_id == organisation_id)
                    # stable ORDER BY (created_at desc, id desc) for a deterministic page (WP-10)
                    .order_by(IntegrationKey.created_at.desc(), IntegrationKey.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
                return list(result.scalars().all())

    async def get_for_org(
        self, *, key_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> IntegrationKey | None:
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(IntegrationKey).where(
                        IntegrationKey.id == key_id,
                        IntegrationKey.organisation_id == organisation_id,
                    )
                )
                return result.scalar_one_or_none()

    async def rotate(
        self,
        *,
        key_id: uuid.UUID,
        organisation_id: uuid.UUID,
        key_prefix: str,
        key_hash: str,
        last4: str,
    ) -> IntegrationKey | None:
        """Replace the secret material in place — instantly invalidates the old key. Org-scoped.

        Rotate is for live keys only: a revoked key is a terminal tombstone, NOT resurrected by a
        rotate (the active-status guard returns None -> the route 404s), so revoke stays final.
        """
        with org_scope(organisation_id):
            async with self._session() as session, session.begin():
                row = await session.get(IntegrationKey, key_id)
                if row is None or row.organisation_id != organisation_id or row.status != "active":
                    return None
                row.key_prefix = key_prefix
                row.key_hash = key_hash
                row.last4 = last4
                session.add(row)
            return row

    async def revoke(
        self, *, key_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> IntegrationKey | None:
        """Soft tombstone — status -> revoked. Org-scoped."""
        with org_scope(organisation_id):
            async with self._session() as session, session.begin():
                row = await session.get(IntegrationKey, key_id)
                if row is None or row.organisation_id != organisation_id:
                    return None
                row.status = "revoked"
                session.add(row)
            return row
