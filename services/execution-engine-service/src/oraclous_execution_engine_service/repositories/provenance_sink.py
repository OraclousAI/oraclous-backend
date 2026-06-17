"""Postgres provenance sink (ORAA-4 §21 repositories layer; CLAUDE.md §3.7).

The concrete ``ProvenanceSink`` behind the substrate ``ProvenanceCollector``. It is the only place
that persists engine provenance (no direct-to-store bypass elsewhere).
"""

from __future__ import annotations

import uuid

from oraclous_substrate import ProvenanceRecord, ProvenanceSink
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from oraclous_execution_engine_service.core.rls import install_org_guc_guard
from oraclous_execution_engine_service.models.provenance import EngineProvenanceEvent


class PostgresProvenanceSink(ProvenanceSink):
    def __init__(
        self, db_url: str, *, worker_pool: bool = False, install_guard: bool = True
    ) -> None:
        kwargs = {"poolclass": NullPool} if worker_pool else {}
        self._engine = create_async_engine(db_url, echo=False, **kwargs)
        # ADR-030 §2: engine_provenance is FORCE'd-RLS, so the write must bind the row's org. The
        # org-bound engine's GUC guard binds it from the bound OrganisationContext — the request +
        # the org-bound task execution bind it via use_organisation_context, and the cross-org
        # sweeps wrap each provenance write in org_scope(row.org) (it fires inside the per-row
        # settle). write() also passes the org explicitly (WITH CHECK admits it).
        if install_guard:
            install_org_guc_guard(self._engine)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def write(self, record: ProvenanceRecord) -> None:
        row = EngineProvenanceEvent(
            id=uuid.uuid4(),
            organisation_id=uuid.UUID(record.organisation_id),
            principal=record.principal,
            action=record.action,
            resource=record.resource,
            outcome=record.outcome,
        )
        async with self._session() as session:
            async with session.begin():
                session.add(row)
