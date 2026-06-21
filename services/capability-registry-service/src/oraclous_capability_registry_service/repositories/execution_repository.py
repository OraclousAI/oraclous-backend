"""Execution repository (repositories layer).

The only DB seam for execution provenance. Every read/write is org-scoped (ADR-006). Stores the
credential *refs* (types/scopes) used, never the secret material.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from oraclous_capability_registry_service.core.rls import build_rls_engine, org_scope
from oraclous_capability_registry_service.models.enums import ExecutionStatus
from oraclous_capability_registry_service.models.execution import Execution


class ExecutionRepository:
    def __init__(self, db_url: str) -> None:
        # ADR-030: RLS org-GUC begin-guard installed on the engine (every tx binds the org).
        self._engine = build_rls_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create_queued(
        self,
        *,
        organisation_id: uuid.UUID,
        instance_id: uuid.UUID,
        capability_id: uuid.UUID,
        user_id: uuid.UUID,
        input_data: dict[str, Any],
        credential_refs: list[dict[str, Any]],
    ) -> Execution:
        row = Execution(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            instance_id=instance_id,
            capability_id=capability_id,
            user_id=user_id,
            status=ExecutionStatus.QUEUED,
            input_data=input_data,
            credential_refs=credential_refs,
        )
        # ADR-030: bind the caller's org so the engine begin-guard sets app.current_organisation_id;
        # without it the FORCE'd RLS WITH CHECK denies the INSERT (42501) under oraclous_app.
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    session.add(row)
                await session.refresh(row)
                return row

    async def finalize(
        self,
        *,
        execution_id: uuid.UUID,
        organisation_id: uuid.UUID,
        status: ExecutionStatus,
        output_data: dict[str, Any] | None,
        error_message: str | None,
        error_type: str | None,
        credits_consumed: Decimal,
        processing_time_ms: int | None,
    ) -> Execution | None:
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    result = await session.execute(
                        select(Execution).where(
                            Execution.id == execution_id,
                            Execution.organisation_id == organisation_id,
                        )
                    )
                    row = result.scalars().first()
                    if row is None:
                        return None
                    row.status = status
                    row.output_data = output_data
                    row.error_message = error_message
                    row.error_type = error_type
                    row.credits_consumed = credits_consumed
                    # processing_time_ms is integral ms; the Numeric(asdecimal) column reads back as
                    # Decimal, so the int write widens at the type level only (SQLAlchemy coerces).
                    row.processing_time_ms = cast("Decimal | None", processing_time_ms)
                await session.refresh(row)
                return row

    async def get_by_id(
        self, execution_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> Execution | None:
        # ADR-030: bind the caller's org so RLS returns this org's rows (else the empty GUC → zero
        # rows). The app-layer organisation_id predicate stays as defense-in-depth.
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(Execution).where(
                        Execution.id == execution_id,
                        Execution.organisation_id == organisation_id,
                    )
                )
                return result.scalars().first()
