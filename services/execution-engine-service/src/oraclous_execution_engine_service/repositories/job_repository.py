"""Engine job repository (ORAA-4 §21 repositories layer).

The only DB seam for engine job rows. Every read/write is org-scoped (ADR-006): writes carry the
resolved ``organisation_id`` and reads filter on it, so a tenant never reads another's jobs.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_execution_engine_service.models.enums import EngineJobState
from oraclous_execution_engine_service.models.job import EngineJob


class JobRepository:
    def __init__(self, db_url: str) -> None:
        self._engine = create_async_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        input_text: str,
        manifest_ref: str | None = None,
        manifest_inline: dict[str, Any] | None = None,
        max_retries: int = 0,
        timeout_seconds: int | None = None,
        schedule_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
    ) -> EngineJob:
        row = EngineJob(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            user_id=user_id,
            state=EngineJobState.QUEUED.value,
            manifest_ref=manifest_ref,
            manifest_inline=manifest_inline,
            input_text=input_text,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
            schedule_id=schedule_id,
            idempotency_key=idempotency_key,
            progress=0,
            retry_count=0,
        )
        async with self._session() as session:
            async with session.begin():
                session.add(row)
            await session.refresh(row)
            return row

    async def get(self, job_id: uuid.UUID, organisation_id: uuid.UUID) -> EngineJob | None:
        async with self._session() as session:
            result = await session.execute(
                select(EngineJob).where(
                    EngineJob.id == job_id, EngineJob.organisation_id == organisation_id
                )
            )
            return result.scalar_one_or_none()

    async def update(
        self, job_id: uuid.UUID, organisation_id: uuid.UUID, **fields: Any
    ) -> EngineJob | None:
        """Patch the given columns on an org-scoped job row, returning the updated row."""
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(EngineJob).where(
                        EngineJob.id == job_id, EngineJob.organisation_id == organisation_id
                    )
                )
                row = result.scalar_one_or_none()
                if row is None:
                    return None
                for key, value in fields.items():
                    setattr(row, key, value)
            await session.refresh(row)
            return row

    async def list_for_org(
        self, organisation_id: uuid.UUID, *, state: str | None = None, limit: int = 50
    ) -> list[EngineJob]:
        stmt = select(EngineJob).where(EngineJob.organisation_id == organisation_id)
        if state is not None:
            stmt = stmt.where(EngineJob.state == state)
        stmt = stmt.order_by(EngineJob.created_at.desc()).limit(limit)
        async with self._session() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())
