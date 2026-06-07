"""Engine job repository (ORAA-4 §21 repositories layer).

The only DB seam for engine job rows. Every read/write is org-scoped (ADR-006): writes carry the
resolved ``organisation_id`` and reads filter on it, so a tenant never reads another's jobs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from oraclous_execution_engine_service.models.enums import EngineJobState
from oraclous_execution_engine_service.models.job import EngineJob


class JobRepository:
    def __init__(self, db_url: str, *, worker_pool: bool = False) -> None:
        # NullPool in the Celery worker: a task owns its connection and disposes it (ADR-012); never
        # share a pool across tasks. The request path uses the default pool.
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

    async def transition(
        self,
        job_id: uuid.UUID,
        organisation_id: uuid.UUID,
        *,
        new_state: str,
        allowed_from: frozenset[str],
        **fields: Any,
    ) -> tuple[EngineJob | None, bool]:
        """Atomic state transition under a row lock (so a concurrent cancel can't race the worker).

        Returns ``(row, applied)``: ``applied`` is False if the row is missing or its current state
        is not in ``allowed_from`` (the transition is a no-op — e.g. a terminal/cancelled job)."""
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(EngineJob)
                    .where(EngineJob.id == job_id, EngineJob.organisation_id == organisation_id)
                    .with_for_update()
                )
                row = result.scalar_one_or_none()
                if row is None:
                    return None, False
                if row.state not in allowed_from:
                    return row, False
                row.state = new_state
                for key, value in fields.items():
                    setattr(row, key, value)
            await session.refresh(row)
            return row, True

    async def get(self, job_id: uuid.UUID, organisation_id: uuid.UUID) -> EngineJob | None:
        async with self._session() as session:
            result = await session.execute(
                select(EngineJob).where(
                    EngineJob.id == job_id, EngineJob.organisation_id == organisation_id
                )
            )
            return result.scalar_one_or_none()

    async def list_stale_running(
        self, older_than: datetime, *, limit: int = 100
    ) -> list[EngineJob]:
        """RUNNING jobs whose last update predates the lease — the reaper's system cross-org sweep.
        NOT org-scoped by design; each row is settled under its own org afterwards."""
        async with self._session() as session:
            result = await session.execute(
                select(EngineJob)
                .where(
                    EngineJob.state == EngineJobState.RUNNING.value,
                    EngineJob.updated_at < older_than,
                )
                .limit(limit)
            )
            return list(result.scalars().all())

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
