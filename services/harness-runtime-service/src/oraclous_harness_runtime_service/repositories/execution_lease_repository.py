"""Execution lease repository (repositories layer).

The only DB seam for the cross-replica cancel lease (#1072 design ruling): a Postgres row is the
signal a `POST /v1/harnesses/{id}/cancel` on one Helm replica uses to reach the replica that is
actually running the loop, since the service has no Redis. Every method is org-scoped (ADR-006):
``create`` writes the resolved ``organisation_id``; ``request_cancel``, ``is_cancel_requested`` and
``release`` all filter on it, so a forged/guessed execution_id from another org can neither flip the
flag, observe it, nor delete the row — the FORCE'd RLS policy on ``harness_execution_leases``
(migration 0010) backstops the same scoping at the database layer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from oraclous_harness_runtime_service.core.rls import build_rls_engine, org_scope
from oraclous_harness_runtime_service.models.execution_lease import HarnessExecutionLease


class DuplicateExecutionId(Exception):
    """Raised by :meth:`ExecutionLeaseRepository.create` when ``execution_id`` already has a lease
    row — the global primary-key conflict the #1072 design ruling maps to 409 on ``execute()``."""

    def __init__(self, execution_id: uuid.UUID) -> None:
        self.execution_id = execution_id
        super().__init__(f"execution_id already claimed: {execution_id}")


class ExecutionLeaseRepository:
    def __init__(self, db_url: str) -> None:
        # ADR-030: build_rls_engine installs the org-GUC begin-guard so every transaction binds the
        # org transaction-locally (fail-closed to the empty GUC when none is bound).
        self._engine = build_rls_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(self, execution_id: uuid.UUID, organisation_id: uuid.UUID) -> None:
        """Insert the lease row before the loop starts. Raises :class:`DuplicateExecutionId` on a
        PK conflict — a caller-supplied id already claimed by any org (including a finished run of
        another org), never silently overwritten."""
        row = HarnessExecutionLease(execution_id=execution_id, organisation_id=organisation_id)
        try:
            # ADR-030: bind the org so the engine begin-guard sets app.current_organisation_id; the
            # FORCE'd RLS WITH CHECK admits this INSERT only when the stamped org equals the bound
            # one.
            with org_scope(organisation_id):
                async with self._session() as session:
                    async with session.begin():
                        session.add(row)
        except IntegrityError as exc:
            raise DuplicateExecutionId(execution_id) from exc

    async def request_cancel(self, execution_id: uuid.UUID, organisation_id: uuid.UUID) -> bool:
        """Flip ``cancel_requested_at`` for the caller's own org. Returns ``True`` only when a row
        owned by ``organisation_id`` exists (already flagged counts too — idempotent) — a wrong org
        or an unknown id returns ``False`` and never touches the flag, so a cross-org caller learns
        nothing about whether the id even exists (H1/H4)."""
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    result = await session.execute(
                        select(HarnessExecutionLease)
                        .where(
                            HarnessExecutionLease.execution_id == execution_id,
                            HarnessExecutionLease.organisation_id == organisation_id,
                        )
                        .with_for_update()
                    )
                    row = result.scalar_one_or_none()
                    if row is None:
                        return False
                    if row.cancel_requested_at is None:
                        row.cancel_requested_at = datetime.now(UTC)
                return True

    async def is_cancel_requested(
        self, execution_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> bool:
        """The watcher's poll: ``True`` only when the row is owned by ``organisation_id`` and its
        flag is set. A wrong/unbound org sees ``False`` under RLS — the same "zero rows" a real
        cross-org watcher would see in production."""
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(HarnessExecutionLease.cancel_requested_at).where(
                        HarnessExecutionLease.execution_id == execution_id,
                        HarnessExecutionLease.organisation_id == organisation_id,
                    )
                )
                flag_value = result.scalar_one_or_none()
                return flag_value is not None

    async def release(self, execution_id: uuid.UUID, organisation_id: uuid.UUID) -> None:
        """Delete the lease row outright, org-scoped — the watcher's cleanup once the terminal row
        is persisted. A wrong org deletes zero rows; the owning org's lease is unaffected."""
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    await session.execute(
                        delete(HarnessExecutionLease).where(
                            HarnessExecutionLease.execution_id == execution_id,
                            HarnessExecutionLease.organisation_id == organisation_id,
                        )
                    )
