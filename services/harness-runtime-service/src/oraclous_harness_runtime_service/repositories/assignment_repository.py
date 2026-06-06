"""Human-actor assignment repository (ORAA-4 §21 repositories layer). Org-scoped (ADR-006)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_harness_runtime_service.models.assignment import HarnessAssignment


class AssignmentRepository:
    def __init__(self, db_url: str) -> None:
        self._engine = create_async_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(
        self,
        *,
        organisation_id: uuid.UUID,
        execution_id: uuid.UUID,
        harness_id: uuid.UUID,
        human_role: str,
        input_text: str,
    ) -> HarnessAssignment:
        row = HarnessAssignment(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            execution_id=execution_id,
            harness_id=harness_id,
            human_role=human_role,
            status="PENDING",
            input=input_text,
        )
        async with self._session() as session:
            async with session.begin():
                session.add(row)
            await session.refresh(row)
            return row

    async def list_for_org(
        self, organisation_id: uuid.UUID, *, status: str | None = None, limit: int = 100
    ) -> list[HarnessAssignment]:
        stmt = select(HarnessAssignment).where(HarnessAssignment.organisation_id == organisation_id)
        if status is not None:
            stmt = stmt.where(HarnessAssignment.status == status)
        stmt = stmt.order_by(HarnessAssignment.created_at.desc()).limit(limit)
        async with self._session() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())
