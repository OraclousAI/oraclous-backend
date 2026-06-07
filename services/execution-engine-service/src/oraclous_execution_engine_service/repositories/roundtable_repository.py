"""Round-table repository (ORAA-4 §21 repositories layer). Org-scoped (ADR-006)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from oraclous_execution_engine_service.models.roundtable import EngineRoundtable


class RoundtableRepository:
    def __init__(self, db_url: str, *, worker_pool: bool = False) -> None:
        kwargs = {"poolclass": NullPool} if worker_pool else {}
        self._engine = create_async_engine(db_url, echo=False, **kwargs)
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

    async def update(
        self, roundtable_id: uuid.UUID, organisation_id: uuid.UUID, **fields: Any
    ) -> EngineRoundtable | None:
        """Patch an org-scoped round-table (state/current_turn/transcript/final_output) under a row
        lock — the drive loop advances it turn by turn."""
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
