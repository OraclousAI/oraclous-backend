"""Harness execution repository (repositories layer).

The only DB seam for harness execution rows. Every read/write is org-scoped (ADR-006): writes carry
the resolved ``organisation_id`` and reads filter on it, so a tenant never reads another's runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from oraclous_harness_runtime_service.core.rls import build_rls_engine, org_scope
from oraclous_harness_runtime_service.models.execution import HarnessExecution


@dataclass(frozen=True, slots=True)
class ModelSpendRow:
    """One aggregated per-model usage row for an org's executions (raw tokens — no price). ``model``
    is the OHM model binding (``None`` for fake-mode runs that recorded no model)."""

    model: str | None
    input_tokens: int
    output_tokens: int
    executions: int


class ExecutionRepository:
    def __init__(self, db_url: str) -> None:
        # ADR-030: build_rls_engine installs the org-GUC begin-guard so every transaction binds the
        # org transaction-locally (fail-closed to the empty GUC when none is bound).
        self._engine = build_rls_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    @property
    def engine(self) -> AsyncEngine:
        """The RLS-guarded engine — the lifespan asserts the runtime role against it at startup
        (all four harness repositories build on the same DSN/role, so one proves the role)."""
        return self._engine

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
        model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        trace_id: uuid.UUID | None = None,
        parent_execution_id: uuid.UUID | None = None,
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
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            steps=steps,
            # run-tree correlation (#471): root mints trace_id = its own id when none was passed.
            trace_id=trace_id if trace_id is not None else execution_id,
            parent_execution_id=parent_execution_id,
        )
        # ADR-030: bind the org so the engine begin-guard sets app.current_organisation_id; the
        # FORCE'd RLS WITH CHECK admits this INSERT only when the stamped org equals the bound one.
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    session.add(row)
                await session.refresh(row)
                return row

    async def get(
        self, execution_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> HarnessExecution | None:
        with org_scope(organisation_id):
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
        with org_scope(organisation_id):
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
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> HarnessExecution | None:
        """Full in-place update of an org-scoped run — the S6 resume path overwrites status/output/
        error/iterations/tokens and REPLACES the step trace (caller appends the new tail). Unlike
        update_status this sets error fields verbatim (a DENIED resume preserves human_rejected).
        ``input_tokens``/``output_tokens`` are updated only when supplied (the spend breakdown)."""
        with org_scope(organisation_id):
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
                    if input_tokens is not None:
                        row.input_tokens = input_tokens
                    if output_tokens is not None:
                        row.output_tokens = output_tokens
                    row.steps = steps
                await session.refresh(row)
                return row

    async def spend_by_model(
        self, organisation_id: uuid.UUID, *, since: datetime | None = None
    ) -> list[ModelSpendRow]:
        """Aggregate the org's executions into per-model sums of input/output tokens + an execution
        count, over an optional ``since`` window (created_at >= since). Org-scoped (ADR-006) — the
        ``organisation_id`` filter is mandatory, so a tenant NEVER sees another org's spend. Returns
        raw token counts only; pricing is a separate read-time layer (``domain.billing.rates``)."""
        with org_scope(organisation_id):
            async with self._session() as session:
                stmt = (
                    select(
                        HarnessExecution.model,
                        func.coalesce(func.sum(HarnessExecution.input_tokens), 0),
                        func.coalesce(func.sum(HarnessExecution.output_tokens), 0),
                        func.count(HarnessExecution.id),
                    )
                    .where(HarnessExecution.organisation_id == organisation_id)
                    .group_by(HarnessExecution.model)
                )
                if since is not None:
                    stmt = stmt.where(HarnessExecution.created_at >= since)
                result = await session.execute(stmt)
                return [
                    ModelSpendRow(
                        model=model,
                        input_tokens=int(input_sum),
                        output_tokens=int(output_sum),
                        executions=int(count),
                    )
                    for model, input_sum, output_sum, count in result.all()
                ]

    async def list_for_org(
        self, organisation_id: uuid.UUID, *, limit: int = 50
    ) -> list[HarnessExecution]:
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(HarnessExecution)
                    .where(HarnessExecution.organisation_id == organisation_id)
                    .order_by(HarnessExecution.created_at.desc())
                    .limit(limit)
                )
                return list(result.scalars().all())
