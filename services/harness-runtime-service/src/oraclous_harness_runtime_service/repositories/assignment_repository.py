"""Human-actor assignment repository (repositories layer). Org-scoped (ADR-006)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from oraclous_harness_runtime_service.core.rls import build_rls_engine, org_scope
from oraclous_harness_runtime_service.models.assignment import HarnessAssignment


class AssignmentRepository:
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
        # ADR-030: bind the org so the engine begin-guard sets the GUC; the FORCE'd RLS WITH CHECK
        # admits this INSERT only because the stamped org equals the bound one.
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    session.add(row)
                await session.refresh(row)
                return row

    async def _set_status(
        self,
        assignment_id: uuid.UUID,
        organisation_id: uuid.UUID,
        *,
        allowed_from: frozenset[str],
        new_status: str,
    ) -> HarnessAssignment | None:
        """Org-scoped status transition under a row lock; None if missing or not in allowed_from."""
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    result = await session.execute(
                        select(HarnessAssignment)
                        .where(
                            HarnessAssignment.id == assignment_id,
                            HarnessAssignment.organisation_id == organisation_id,
                        )
                        .with_for_update()
                    )
                    row = result.scalar_one_or_none()
                    if row is None or row.status not in allowed_from:
                        return None
                    row.status = new_status
                await session.refresh(row)
                return row

    async def claim(
        self, assignment_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> HarnessAssignment | None:
        """A human takes the task: PENDING → CLAIMED."""
        return await self._set_status(
            assignment_id,
            organisation_id,
            allowed_from=frozenset({"PENDING"}),
            new_status="CLAIMED",
        )

    async def complete(
        self, assignment_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> HarnessAssignment | None:
        """The human finished: PENDING/CLAIMED → COMPLETED (the row carries the execution_id)."""
        return await self._set_status(
            assignment_id,
            organisation_id,
            allowed_from=frozenset({"PENDING", "CLAIMED"}),
            new_status="COMPLETED",
        )

    async def list_for_org(
        self, organisation_id: uuid.UUID, *, status: str | None = None, limit: int = 100
    ) -> list[HarnessAssignment]:
        stmt = select(HarnessAssignment).where(HarnessAssignment.organisation_id == organisation_id)
        if status is not None:
            stmt = stmt.where(HarnessAssignment.status == status)
        stmt = stmt.order_by(HarnessAssignment.created_at.desc()).limit(limit)
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(stmt)
                return list(result.scalars().all())
