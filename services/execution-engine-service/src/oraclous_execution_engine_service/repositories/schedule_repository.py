"""Engine schedule repository (ORAA-4 §21 repositories layer).

The only DB seam for engine schedule rows. Org-scoped (ADR-006) for the API; ``list_enabled_cron``
is the reaper-style system sweep Celery Beat fires from (cross-org maintenance — same ADR-006
carve-out as the job reaper; each fired job is created under its schedule's OWN org).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from oraclous_execution_engine_service.core.rls import install_org_guc_guard
from oraclous_execution_engine_service.models.enums import ScheduleType
from oraclous_execution_engine_service.models.schedule import EngineSchedule


class ScheduleRepository:
    def __init__(
        self, db_url: str, *, worker_pool: bool = False, install_guard: bool = True
    ) -> None:
        kwargs = {"poolclass": NullPool} if worker_pool else {}
        self._engine = create_async_engine(db_url, echo=False, **kwargs)
        # ADR-030 §2: org-bound engine carries the org-GUC guard (see JobRepository). The Beat
        # cross-org read (list_enabled_cron) uses the MAINTENANCE reader on the owner engine
        # instead; set_last_fired is settled per-row under org_scope on the org-bound engine.
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
        type: str,
        input_text: str,
        manifest_inline: dict | None = None,
        manifest_ref: str | None = None,
        cron: str | None = None,
    ) -> EngineSchedule:
        row = EngineSchedule(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            user_id=user_id,
            type=type,
            cron=cron,
            manifest_inline=manifest_inline,
            manifest_ref=manifest_ref,
            input_text=input_text,
            enabled=True,
        )
        async with self._session() as session:
            async with session.begin():
                session.add(row)
            await session.refresh(row)
            return row

    async def get(
        self, schedule_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> EngineSchedule | None:
        async with self._session() as session:
            result = await session.execute(
                select(EngineSchedule).where(
                    EngineSchedule.id == schedule_id,
                    EngineSchedule.organisation_id == organisation_id,
                )
            )
            return result.scalar_one_or_none()

    async def list_for_org(
        self, organisation_id: uuid.UUID, *, limit: int = 100
    ) -> list[EngineSchedule]:
        async with self._session() as session:
            result = await session.execute(
                select(EngineSchedule)
                .where(EngineSchedule.organisation_id == organisation_id)
                .order_by(EngineSchedule.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def delete(self, schedule_id: uuid.UUID, organisation_id: uuid.UUID) -> bool:
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    sa_delete(EngineSchedule).where(
                        EngineSchedule.id == schedule_id,
                        EngineSchedule.organisation_id == organisation_id,
                    )
                )
            return result.rowcount > 0

    async def list_enabled_cron(self, *, limit: int = 500) -> list[EngineSchedule]:
        """System sweep (Celery Beat): all enabled cron schedules across orgs. Each fired job is
        created under its schedule's own org (ADR-006 carve-out, like the job reaper)."""
        async with self._session() as session:
            result = await session.execute(
                select(EngineSchedule)
                .where(
                    EngineSchedule.type == ScheduleType.CRON.value,
                    EngineSchedule.enabled.is_(True),
                )
                .limit(limit)
            )
            return list(result.scalars().all())

    async def set_last_fired(self, schedule_id: uuid.UUID, fired_at: datetime) -> None:
        async with self._session() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(EngineSchedule).where(EngineSchedule.id == schedule_id)
                    )
                ).scalar_one_or_none()
                if row is not None:
                    row.last_fired_at = fired_at
