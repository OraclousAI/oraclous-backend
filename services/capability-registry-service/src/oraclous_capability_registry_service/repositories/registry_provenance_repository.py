"""Registry provenance read repository (repositories layer).

The read-only seam over ``registry_provenance`` — this service's own §3.7 audit/event log. The write
side is the substrate ``ProvenanceSink`` (``registry_provenance_sink.py``); this is the only place
reads of those rows happen. Strictly org-isolated like ``ExecutionRepository``: every method binds
the caller's org via ``org_scope`` itself (never hand-bound by the caller), so the FORCE'd RLS
backstop scopes the read even if the app-layer predicate were ever dropped (ADR-006, ADR-030 §1).
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from oraclous_capability_registry_service.core.rls import build_rls_engine, org_scope
from oraclous_capability_registry_service.models.registry_provenance import RegistryProvenance


class RegistryProvenanceRepository:
    def __init__(self, db_url: str) -> None:
        # ADR-030: RLS org-GUC begin-guard installed on the engine (every tx binds the org).
        self._engine = build_rls_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def list_by_org(
        self, organisation_id: uuid.UUID, *, limit: int
    ) -> list[RegistryProvenance]:
        """The org's most-recent provenance events, newest-first. ``limit``-capped. Org-scoped both
        at the app layer (the ``WHERE``) and via the bound org-GUC (the RLS backstop) — a caller
        that forgets to bind the org still fails closed to zero rows, never another org's rows."""
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(RegistryProvenance)
                    .where(RegistryProvenance.organisation_id == organisation_id)
                    .order_by(RegistryProvenance.created_at.desc())
                    .limit(limit)
                )
                return list(result.scalars().all())

    async def count_by_org(self, organisation_id: uuid.UUID) -> int:
        """The org's full event count, independent of any page ``limit`` (11 Sep ruling)."""
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(func.count())
                    .select_from(RegistryProvenance)
                    .where(RegistryProvenance.organisation_id == organisation_id)
                )
                return int(result.scalar_one())
