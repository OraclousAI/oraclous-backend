"""Mid-loop HITL checkpoint repository (ORAA-4 §21 repositories layer). Org-scoped (ADR-006)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from oraclous_harness_runtime_service.core.rls import build_rls_engine, org_scope
from oraclous_harness_runtime_service.models.checkpoint import HarnessCheckpoint


class CheckpointRepository:
    def __init__(self, db_url: str) -> None:
        # ADR-030: RLS org-GUC begin-guard installed on the engine (every tx binds the org). One of
        # the four independent harness repository engines — each must be built through here.
        self._engine = build_rls_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(
        self,
        *,
        organisation_id: uuid.UUID,
        execution_id: uuid.UUID,
        manifest_doc: dict[str, Any],
        resume_messages: list[dict[str, Any]],
        pending_tool_calls: list[dict[str, Any]],
        approved_tool_call_id: str,
        resume_cursor: dict[str, int],
        redact_patterns: list[str],
    ) -> HarnessCheckpoint:
        row = HarnessCheckpoint(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            execution_id=execution_id,
            status="PENDING",
            manifest_doc=manifest_doc,
            resume_messages=resume_messages,
            pending_tool_calls=pending_tool_calls,
            approved_tool_call_id=approved_tool_call_id,
            resume_cursor=resume_cursor,
            redact_patterns=redact_patterns,
        )
        # ADR-030: bind the org so the engine begin-guard sets the GUC; the FORCE'd RLS WITH CHECK
        # admits this INSERT only because the stamped org equals the bound one.
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    session.add(row)
                await session.refresh(row)
                return row

    async def get_latest_pending(
        self, execution_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> HarnessCheckpoint | None:
        """The most recent PENDING checkpoint for a run (a chained-gate run has one per pause)."""
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(HarnessCheckpoint)
                    .where(
                        HarnessCheckpoint.execution_id == execution_id,
                        HarnessCheckpoint.organisation_id == organisation_id,
                        HarnessCheckpoint.status == "PENDING",
                    )
                    .order_by(HarnessCheckpoint.created_at.desc())
                    .limit(1)
                )
                return result.scalar_one_or_none()

    async def set_decision(
        self, checkpoint_id: uuid.UUID, organisation_id: uuid.UUID, new_status: str
    ) -> HarnessCheckpoint | None:
        """CAS PENDING → APPROVED/DENIED under a row lock; None if missing or already decided —
        so a decision is applied exactly once even under a concurrent approve."""
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    result = await session.execute(
                        select(HarnessCheckpoint)
                        .where(
                            HarnessCheckpoint.id == checkpoint_id,
                            HarnessCheckpoint.organisation_id == organisation_id,
                        )
                        .with_for_update()
                    )
                    row = result.scalar_one_or_none()
                    if row is None or row.status != "PENDING":
                        return None
                    row.status = new_status
                await session.refresh(row)
                return row

    async def revert_to_pending(self, checkpoint_id: uuid.UUID, organisation_id: uuid.UUID) -> None:
        """Compensation: un-claim a decision when the resume that claimed it then failed — so the
        run is retryable instead of stranded ESCALATED with a no-longer-PENDING checkpoint."""
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    result = await session.execute(
                        select(HarnessCheckpoint)
                        .where(
                            HarnessCheckpoint.id == checkpoint_id,
                            HarnessCheckpoint.organisation_id == organisation_id,
                        )
                        .with_for_update()
                    )
                    row = result.scalar_one_or_none()
                    if row is not None:
                        row.status = "PENDING"
