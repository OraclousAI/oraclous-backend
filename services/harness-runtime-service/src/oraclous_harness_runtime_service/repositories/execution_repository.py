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
        content_hash: str | None,
        status: str,
        input_text: str,
        output: str | None,
        error_type: str | None,
        error_message: str | None,
        iterations: int,
        total_tokens: int,
        steps: list[dict[str, Any]],
    ) -> HarnessExecution:
        row = HarnessExecution(
            id=execution_id,
            organisation_id=organisation_id,
            user_id=user_id,
            harness_id=harness_id,
            harness_name=harness_name,
            content_hash=content_hash,
            status=status,
            input=input_text,
            output=output,
            error_type=error_type,
            error_message=error_message,
            iterations=iterations,
            total_tokens=total_tokens,
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

    async def update_status(
        self,
        execution_id: uuid.UUID,
        organisation_id: uuid.UUID,
        *,
        status: str,
        output: str | None = None,
    ) -> HarnessExecution | None:
        """Patch an org-scoped run's status (+ output) — when a human completes an assignment, flip
        the parked ESCALATED run to SUCCEEDED with the human's output. Org-scoped (ADR-006)."""
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(HarnessExecution)
                    .where(
                        HarnessExecution.id == execution_id,
                        HarnessExecution.organisation_id == organisation_id,
                    )
                    .with_for_update()
                )
                row = result.scalar_one_or_none()
                if row is None:
                    return None
                row.status = status
                if output is not None:
                    row.output = output
                row.error_type = None
                row.error_message = None
            await session.refresh(row)
            return row

    async def update_run(
        self,
        execution_id: uuid.UUID,
        organisation_id: uuid.UUID,
        *,
        status: str,
        output: str | None,
        error_type: str | None,
        error_message: str | None,
        iterations: int,
        total_tokens: int,
        steps: list[dict[str, Any]],
    ) -> HarnessExecution | None:
        """Full in-place update of an org-scoped run — the S6 resume path overwrites status/output/
        error/iterations/tokens and REPLACES the step trace (caller appends the new tail). Unlike
        update_status this sets error fields verbatim (a DENIED resume preserves human_rejected)."""
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(HarnessExecution)
                    .where(
                        HarnessExecution.id == execution_id,
                        HarnessExecution.organisation_id == organisation_id,
                    )
                    .with_for_update()
                )
                row = result.scalar_one_or_none()
                if row is None:
                    return None
                row.status = status
                row.output = output
                row.error_type = error_type
                row.error_message = error_message
                row.iterations = iterations
                row.total_tokens = total_tokens
                row.steps = steps
            await session.refresh(row)
            return row

    async def list_for_org(
        self, organisation_id: uuid.UUID, *, limit: int = 50
    ) -> list[HarnessExecution]:
        async with self._session() as session:
            result = await session.execute(
                select(HarnessExecution)
                .where(HarnessExecution.organisation_id == organisation_id)
                .order_by(HarnessExecution.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())
