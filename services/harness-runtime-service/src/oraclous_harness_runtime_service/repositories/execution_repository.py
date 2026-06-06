"""Harness execution repository (ORAA-4 §21 repositories layer).

The only DB seam for harness execution rows. Every read/write is org-scoped (ADR-006): writes carry
the resolved ``organisation_id`` and reads filter on it, so a tenant never reads another's runs.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_harness_runtime_service.models.execution import HarnessExecution


class ExecutionRepository:
    def __init__(self, db_url: str) -> None:
        self._engine = create_async_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(
        self,
        *,
        execution_id: uuid.UUID,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        harness_id: uuid.UUID,
        harness_name: str,
        status: str,
        input_text: str,
        output: str | None,
        error_type: str | None,
        error_message: str | None,
        iterations: int,
        steps: list[dict[str, Any]],
    ) -> HarnessExecution:
        row = HarnessExecution(
            id=execution_id,
            organisation_id=organisation_id,
            user_id=user_id,
            harness_id=harness_id,
            harness_name=harness_name,
            status=status,
            input=input_text,
            output=output,
            error_type=error_type,
            error_message=error_message,
            iterations=iterations,
            steps=steps,
        )
        async with self._session() as session:
            async with session.begin():
                session.add(row)
            await session.refresh(row)
            return row

    async def get(
        self, execution_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> HarnessExecution | None:
        async with self._session() as session:
            result = await session.execute(
                select(HarnessExecution).where(
                    HarnessExecution.id == execution_id,
                    HarnessExecution.organisation_id == organisation_id,
                )
            )
            return result.scalar_one_or_none()
