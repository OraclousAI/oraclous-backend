"""Team-run repository (repositories layer). Org-scoped (ADR-006); the org-GUC guard
(ADR-030) is installed on the engine, so every query is RLS-backstopped on the ``oraclous_app``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from oraclous_execution_engine_service.core.rls import install_org_guc_guard
from oraclous_execution_engine_service.models.team_run import EngineTeamRun

# #598: the non-terminal team-run states — a run in one of these has NOT yet accrued its final cost.
# The L3 per-period budget guard serialises a budgeted standing team on this set (see
# ScheduleService._budget_preflight), so the cap is never overrun by dispatched-but-unsettled runs.
_ACTIVE_TEAM_RUN_STATES = frozenset({"QUEUED", "RUNNING", "PAUSED"})


class TeamRunRepository:
    def __init__(
        self, db_url: str, *, worker_pool: bool = False, install_guard: bool = True
    ) -> None:
        kwargs = {"poolclass": NullPool} if worker_pool else {}
        self._engine = create_async_engine(db_url, echo=False, **kwargs)
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
        manifest: dict[str, Any],
        sub_harnesses: dict[str, Any],
        gate_decisions: dict[str, Any],
        workspace_root: str | None = None,
        graph_id: str | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> EngineTeamRun:
        row = EngineTeamRun(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            user_id=user_id,
            manifest=manifest,
            sub_harnesses=sub_harnesses,
            gate_decisions=gate_decisions,
            state="QUEUED",
            results={},
            paused_at=[],
            workspace_root=workspace_root,
            graph_id=graph_id,
            inputs=inputs,
        )
        async with self._session() as session:
            async with session.begin():
                session.add(row)
            await session.refresh(row)
            return row

    async def create_scheduled(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        manifest: dict[str, Any],
        sub_harnesses: dict[str, Any],
        gate_decisions: dict[str, Any],
        graph_id: str | None,
        schedule_id: uuid.UUID,
        idempotency_key: str,
    ) -> EngineTeamRun | None:
        """#601: create a QUEUED team-run for a standing-team schedule fire, idempotent on
        ``(org, idempotency_key)`` via the partial unique. Returns ``None`` on a duplicate
        same-window fire (IntegrityError) — the create-BEFORE-enqueue dedupe (mirrors
        ``create_adopted_tool_run``). Bound to the schedule's persistent ``graph_id`` + carrying
        ``schedule_id`` so the worker accrues the settled cost back into the schedule."""
        row = EngineTeamRun(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            user_id=user_id,
            manifest=manifest,
            sub_harnesses=sub_harnesses,
            gate_decisions=gate_decisions,
            state="QUEUED",
            results={},
            paused_at=[],
            graph_id=graph_id,
            schedule_id=schedule_id,
            idempotency_key=idempotency_key,
        )
        try:
            async with self._session() as session:
                async with session.begin():
                    session.add(row)
                await session.refresh(row)
                return row
        except IntegrityError:
            return None

    async def get(self, team_run_id: uuid.UUID, organisation_id: uuid.UUID) -> EngineTeamRun | None:
        async with self._session() as session:
            result = await session.execute(
                select(EngineTeamRun).where(
                    EngineTeamRun.id == team_run_id,
                    EngineTeamRun.organisation_id == organisation_id,
                )
            )
            return result.scalar_one_or_none()

    async def list_for_schedule(
        self, schedule_id: uuid.UUID, organisation_id: uuid.UUID, *, limit: int = 100
    ) -> list[EngineTeamRun]:
        """#601: the team-runs a standing-team schedule produced (org-scoped, newest-first) — the
        readable proof a schedule fired + the persistent graph each run is bound to."""
        async with self._session() as session:
            result = await session.execute(
                select(EngineTeamRun)
                .where(
                    EngineTeamRun.schedule_id == schedule_id,
                    EngineTeamRun.organisation_id == organisation_id,
                )
                .order_by(EngineTeamRun.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def has_active_for_schedule(
        self, schedule_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> bool:
        """#598: True if the schedule has a non-terminal (QUEUED/RUNNING/PAUSED) team-run — a fire
        whose cost has NOT yet accrued. The L3 budget pre-flight serialises a BUDGETED standing team
        on this: it never fires the next window while a prior run is in-flight, so the per-period
        cap is checked against CURRENT settled spend and can never be overrun by dispatched-but-
        unsettled runs (ADR-048 dec 4b: 'does not silently overrun'). Org-scoped."""
        async with self._session() as session:
            result = await session.execute(
                select(func.count())
                .select_from(EngineTeamRun)
                .where(
                    EngineTeamRun.schedule_id == schedule_id,
                    EngineTeamRun.organisation_id == organisation_id,
                    EngineTeamRun.state.in_(_ACTIVE_TEAM_RUN_STATES),
                )
            )
            return (result.scalar_one() or 0) > 0

    async def transition(
        self,
        team_run_id: uuid.UUID,
        organisation_id: uuid.UUID,
        *,
        new_state: str,
        allowed_from: frozenset[str],
        **fields: Any,
    ) -> tuple[EngineTeamRun | None, bool]:
        """CAS the team run into ``new_state`` only if its current state is in ``allowed_from``,
        under a row lock; returns (row, applied). The single-driver claim — a redelivered or
        concurrent driver that finds the run already RUNNING/terminal becomes a no-op."""
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(EngineTeamRun)
                    .where(
                        EngineTeamRun.id == team_run_id,
                        EngineTeamRun.organisation_id == organisation_id,
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

    async def list_stale_running(
        self, older_than: datetime, *, limit: int = 100
    ) -> list[EngineTeamRun]:
        """RUNNING team runs whose last update predates the lease — the reaper's system sweep for a
        driver that died mid-drive. NOT org-scoped (maintenance read on the owner engine); each row
        is failed under its own org via ``org_scope`` (same ADR-006 carve-out as the job reaper)."""
        async with self._session() as session:
            result = await session.execute(
                select(EngineTeamRun)
                .where(
                    EngineTeamRun.state == "RUNNING",
                    EngineTeamRun.updated_at < older_than,
                )
                .limit(limit)
            )
            return list(result.scalars().all())
