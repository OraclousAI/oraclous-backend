"""Engine schedule repository (repositories layer).

The only DB seam for engine schedule rows. Org-scoped (ADR-006) for the API; ``list_enabled_cron``
is the reaper-style system sweep Celery Beat fires from (cross-org maintenance — same ADR-006
carve-out as the job reaper; each fired job is created under its schedule's OWN org).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import cast

from sqlalchemy import CursorResult, select
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
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
        target_kind: str = "harness_job",
        instance_id: uuid.UUID | None = None,
        input_data: dict | None = None,
        graph_id: str | None = None,
        budget_period: str | None = None,
        budget_allowance_tokens: int | None = None,
        budget_window_start: datetime | None = None,
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
            target_kind=target_kind,
            instance_id=instance_id,
            input_data=input_data,
            graph_id=graph_id,  # #601: the standing team's persistent graph workspace (team only)
            # #598: the L3 per-period cap (all NULL => OFF). budget_window_start anchors window 1.
            budget_period=budget_period,
            budget_allowance_tokens=budget_allowance_tokens,
            budget_window_start=budget_window_start,
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
            # AsyncSession.execute is typed Result; a DML statement returns a CursorResult (the only
            # variant carrying ``rowcount``). Cast to read the affected-row count (typed-service
            # convention).
            return (cast("CursorResult[object]", result).rowcount or 0) > 0

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

    async def list_budget_paused(self, *, limit: int = 500) -> list[EngineSchedule]:
        """#598 system sweep (Celery Beat): all schedules L3 paused on a per-period breach, across
        orgs. A paused (enabled=False) schedule is invisible to ``list_enabled_cron``, so the fire
        path can never resume it — this read feeds the boundary re-enable sweep. ``budget_paused``
        (not just enabled=False) so a MANUALLY disabled schedule is never auto-resumed."""
        async with self._session() as session:
            result = await session.execute(
                select(EngineSchedule).where(EngineSchedule.budget_paused.is_(True)).limit(limit)
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

    async def accrue_recurring_cost(
        self, schedule_id: uuid.UUID, organisation_id: uuid.UUID, delta: int
    ) -> None:
        """#601: ATOMICALLY add a settled team-run's RAW cost to the schedule's per-cadence
        accumulator (the accumulator #598's per-period cap reads). An IN-DB increment (not a
        read-modify-write), so two concurrent fire-settles never lose an update. Org-scoped — run
        under ``org_scope(organisation_id)`` so the org-bound engine's GUC admits the UPDATE."""
        if delta <= 0:  # a 0-cost (or degraded) run has nothing to accrue
            return
        async with self._session() as session:
            async with session.begin():
                await session.execute(
                    sa_update(EngineSchedule)
                    .where(
                        EngineSchedule.id == schedule_id,
                        EngineSchedule.organisation_id == organisation_id,
                    )
                    .values(recurring_cost_tokens=EngineSchedule.recurring_cost_tokens + delta)
                )

    async def set_last_settled_run(
        self, schedule_id: uuid.UUID, organisation_id: uuid.UUID, run_id: uuid.UUID
    ) -> None:
        """#544: stamp the schedule's most recent SUCCEEDED team-run — the SEED for the NEXT fire (a
        recurring refresh carries forward the prior fire's records). One org-scoped in-DB UPDATE
        (mirrors ``accrue_recurring_cost``); run under ``org_scope(organisation_id)`` so the org
        engine's GUC admits it. Called best-effort at settle for a SUCCEEDED scheduled fire only — a
        FAILED/PAUSED fire never overwrites the last good seed."""
        async with self._session() as session:
            async with session.begin():
                await session.execute(
                    sa_update(EngineSchedule)
                    .where(
                        EngineSchedule.id == schedule_id,
                        EngineSchedule.organisation_id == organisation_id,
                    )
                    .values(last_settled_team_run_id=run_id)
                )

    async def reset_window(
        self, schedule_id: uuid.UUID, organisation_id: uuid.UUID, new_window_start: datetime
    ) -> None:
        """#598: roll the per-period budget window — zero the in-window accrual, advance the anchor,
        clear the budget-pause, and re-enable. Used BOTH by the fire-path pre-flight (the schedule
        is already enabled, so enabled=True is a no-op) AND by the boundary-resume sweep (which
        flips a budget-paused schedule back to enabled). One atomic UPDATE; org-scoped — run under
        ``org_scope(organisation_id)`` so the org-bound engine's GUC admits it."""
        async with self._session() as session:
            async with session.begin():
                await session.execute(
                    sa_update(EngineSchedule)
                    .where(
                        EngineSchedule.id == schedule_id,
                        EngineSchedule.organisation_id == organisation_id,
                    )
                    .values(
                        recurring_cost_tokens=0,
                        budget_window_start=new_window_start,
                        budget_paused=False,
                        enabled=True,
                    )
                )

    async def pause_budget(self, schedule_id: uuid.UUID, organisation_id: uuid.UUID) -> None:
        """#598: pause the standing fleet on a per-period breach — disable the schedule (the
        existing cron ``enabled`` pause, ADR-048 dec 4b) + mark it budget-paused so the sweep (and
        only the sweep, never a manual disable) auto-resumes it. Atomic + org-scoped."""
        async with self._session() as session:
            async with session.begin():
                await session.execute(
                    sa_update(EngineSchedule)
                    .where(
                        EngineSchedule.id == schedule_id,
                        EngineSchedule.organisation_id == organisation_id,
                    )
                    .values(enabled=False, budget_paused=True)
                )
