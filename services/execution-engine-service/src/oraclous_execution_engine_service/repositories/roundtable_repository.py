"""Round-table repository (ORAA-4 §21 repositories layer). Org-scoped (ADR-006)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from oraclous_execution_engine_service.core.rls import install_org_guc_guard
from oraclous_execution_engine_service.models.roundtable import EngineRoundtable


class RoundtableRepository:
    def __init__(
        self, db_url: str, *, worker_pool: bool = False, install_guard: bool = True
    ) -> None:
        kwargs = {"poolclass": NullPool} if worker_pool else {}
        self._engine = create_async_engine(db_url, echo=False, **kwargs)
        # ADR-030 §2: org-bound engine carries the org-GUC guard (see JobRepository). The reaper
        # cross-org read (list_stale_running) uses the MAINTENANCE reader on the owner engine; each
        # stale row is re-queued per-row under org_scope on the org-bound engine.
        if install_guard:
            install_org_guc_guard(self._engine)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        topic: str,
        actors: list[dict[str, Any]],
        max_rounds: int,
    ) -> EngineRoundtable:
        row = EngineRoundtable(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            user_id=user_id,
            topic=topic,
            actors=actors,
            max_rounds=max_rounds,
            current_turn=0,
            state="QUEUED",
            transcript=[],
        )
        async with self._session() as session:
            async with session.begin():
                session.add(row)
            await session.refresh(row)
            return row

    async def get(
        self, roundtable_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> EngineRoundtable | None:
        async with self._session() as session:
            result = await session.execute(
                select(EngineRoundtable).where(
                    EngineRoundtable.id == roundtable_id,
                    EngineRoundtable.organisation_id == organisation_id,
                )
            )
            return result.scalar_one_or_none()

    async def transition(
        self,
        roundtable_id: uuid.UUID,
        organisation_id: uuid.UUID,
        *,
        new_state: str,
        allowed_from: frozenset[str],
        **fields: Any,
    ) -> tuple[EngineRoundtable | None, bool]:
        """CAS the round-table into ``new_state`` only if its current state is in ``allowed_from``,
        under a row lock; returns (row, applied). This is the single-driver claim — a redelivered or
        concurrent driver that finds the round-table already RUNNING/terminal becomes a no-op."""
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(EngineRoundtable)
                    .where(
                        EngineRoundtable.id == roundtable_id,
                        EngineRoundtable.organisation_id == organisation_id,
                    )
                    .with_for_update()
                )
                row = result.scalar_one_or_none()
                if row is None or row.state not in allowed_from:
                    return row, False
                row.state = new_state
                for key, value in fields.items():
                    setattr(row, key, value)
            if row is not None:
                await session.refresh(row)
            return row, True

    async def update(
        self, roundtable_id: uuid.UUID, organisation_id: uuid.UUID, **fields: Any
    ) -> EngineRoundtable | None:
        """Patch an org-scoped round-table mid-drive (transcript/current_turn) under a lock. Safe
        without a CAS because the QUEUED→RUNNING claim guarantees a single driver holds the row."""
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(EngineRoundtable)
                    .where(
                        EngineRoundtable.id == roundtable_id,
                        EngineRoundtable.organisation_id == organisation_id,
                    )
                    .with_for_update()
                )
                row = result.scalar_one_or_none()
                if row is None:
                    return None
                for key, value in fields.items():
                    setattr(row, key, value)
            await session.refresh(row)
            return row

    async def list_stale_running(
        self, older_than: datetime, *, limit: int = 100
    ) -> list[EngineRoundtable]:
        """RUNNING round-tables whose last update predates the lease — the reaper's system sweep for
        a driver that died mid-turn. NOT org-scoped (maintenance); each row re-queues under its own
        org (same ADR-006 carve-out as the job reaper)."""
        async with self._session() as session:
            result = await session.execute(
                select(EngineRoundtable)
                .where(
                    EngineRoundtable.state == "RUNNING",
                    EngineRoundtable.updated_at < older_than,
                )
                .limit(limit)
            )
            return list(result.scalars().all())
