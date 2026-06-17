"""Engine provenance read repository (ORAA-4 §21 repositories layer).

The read seam over ``engine_provenance`` — the substrate's audit/event log. The write side is the
substrate ``ProvenanceSink`` (``provenance_sink.py``); this is the only place reads of those rows
happen. Every read is org-scoped (ADR-006): the caller's ``organisation_id`` is a mandatory filter,
so a tenant never reads another's events.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from oraclous_execution_engine_service.core.rls import install_org_guc_guard
from oraclous_execution_engine_service.models.provenance import EngineProvenanceEvent


class ProvenanceRepository:
    def __init__(
        self, db_url: str, *, worker_pool: bool = False, install_guard: bool = True
    ) -> None:
        # NullPool in a worker (a task owns + disposes its connection, ADR-012); the request path
        # uses the default pool. Mirrors JobRepository.
        kwargs = {"poolclass": NullPool} if worker_pool else {}
        self._engine = create_async_engine(db_url, echo=False, **kwargs)
        # ADR-030 §2: engine_provenance rides along on the org-bound engine (clean — every read is
        # org-scoped via the request principal; the org-GUC guard scopes it at the data layer too).
        if install_guard:
            install_org_guc_guard(self._engine)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def recent(
        self, organisation_id: uuid.UUID, *, limit: int = 50
    ) -> list[EngineProvenanceEvent]:
        """The org's most-recent provenance events, newest-first. Org-scoped — never another org's
        rows. The caller is responsible for clamping ``limit`` to a sane cap."""
        stmt = (
            select(EngineProvenanceEvent)
            .where(EngineProvenanceEvent.organisation_id == organisation_id)
            .order_by(EngineProvenanceEvent.created_at.desc())
            .limit(limit)
        )
        async with self._session() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def usage_by_action(
        self, organisation_id: uuid.UUID, *, since: datetime | None = None
    ) -> list[tuple[str, int]]:
        """RAW usage signal: ``(action, count)`` pairs for the org, grouped by ``action`` (ADR-009 —
        a count, never a price/USD/credits). Org-scoped; an optional ``since`` lower-bounds the
        window on ``created_at``. Ordered by count desc then action for a stable response."""
        stmt = (
            select(EngineProvenanceEvent.action, func.count().label("count"))
            .where(EngineProvenanceEvent.organisation_id == organisation_id)
            .group_by(EngineProvenanceEvent.action)
            .order_by(func.count().desc(), EngineProvenanceEvent.action.asc())
        )
        if since is not None:
            stmt = stmt.where(EngineProvenanceEvent.created_at >= since)
        async with self._session() as session:
            result = await session.execute(stmt)
            return [(row.action, int(row.count)) for row in result.all()]
