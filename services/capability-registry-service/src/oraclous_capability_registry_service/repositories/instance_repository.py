"""Tool instance repository (repositories layer; reshape of legacy
``oraclous-core-service/app/repositories/instance_repository.py``).

The only DB seam for tool instances. Every read/write is org-scoped (ADR-006) — the organisation is
supplied by the caller from the authenticated principal, never a request body (ORG001).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from oraclous_capability_registry_service.core.rls import build_rls_engine, org_scope
from oraclous_capability_registry_service.models.enums import InstanceStatus
from oraclous_capability_registry_service.models.tool_instance import ToolInstance


class InstanceRepository:
    def __init__(self, db_url: str) -> None:
        # ADR-030: RLS org-GUC begin-guard installed on the engine (every tx binds the org).
        self._engine = build_rls_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(
        self,
        *,
        organisation_id: uuid.UUID,
        capability_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str,
        description: str | None,
        configuration: dict[str, Any],
        settings: dict[str, Any],
        required_credentials: list[str],
        status: InstanceStatus,
    ) -> ToolInstance:
        row = ToolInstance(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            capability_id=capability_id,
            user_id=user_id,
            name=name,
            description=description,
            configuration=configuration,
            settings=settings,
            credential_mappings={},
            required_credentials=required_credentials,
            status=status,
        )
        # ADR-030: bind the caller's org so the engine begin-guard sets app.current_organisation_id;
        # without it the FORCE'd RLS WITH CHECK denies the INSERT (42501) under oraclous_app.
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    session.add(row)
                await session.refresh(row)
                return row

    async def get_by_id(
        self, instance_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> ToolInstance | None:
        # ADR-030: bind the caller's org so RLS returns this org's rows (else the empty GUC → zero
        # rows). The app-layer organisation_id predicate stays as defense-in-depth.
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(ToolInstance).where(
                        ToolInstance.id == instance_id,
                        ToolInstance.organisation_id == organisation_id,
                    )
                )
                return result.scalars().first()

    async def list_by_org(self, organisation_id: uuid.UUID) -> list[ToolInstance]:
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(ToolInstance)
                    .where(ToolInstance.organisation_id == organisation_id)
                    .order_by(ToolInstance.created_at)
                )
                return list(result.scalars().all())

    async def record_execution(
        self,
        instance_id: uuid.UUID,
        organisation_id: uuid.UUID,
        *,
        execution_id: uuid.UUID,
        status: InstanceStatus,
        credits_consumed: Decimal,
    ) -> ToolInstance | None:
        """Bump the instance's execution counters + last_execution_id after a dispatch."""
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    result = await session.execute(
                        select(ToolInstance).where(
                            ToolInstance.id == instance_id,
                            ToolInstance.organisation_id == organisation_id,
                        )
                    )
                    row = result.scalars().first()
                    if row is None:
                        return None
                    row.last_execution_id = execution_id
                    # Numeric(asdecimal) round-trips as Decimal; the ``+ 1`` widens the value to
                    # ``Decimal | int`` at the type level only (SQLAlchemy coerces the write).
                    row.execution_count = cast("Decimal", (row.execution_count or 0) + 1)
                    row.total_credits_consumed = (
                        row.total_credits_consumed or 0
                    ) + credits_consumed
                    row.status = status
                await session.refresh(row)
                return row

    async def set_credentials_and_status(
        self,
        instance_id: uuid.UUID,
        organisation_id: uuid.UUID,
        credential_mappings: dict[str, str],
        status: InstanceStatus,
    ) -> ToolInstance | None:
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    result = await session.execute(
                        select(ToolInstance).where(
                            ToolInstance.id == instance_id,
                            ToolInstance.organisation_id == organisation_id,
                        )
                    )
                    row = result.scalars().first()
                    if row is None:
                        return None
                    row.credential_mappings = credential_mappings
                    row.status = status
                await session.refresh(row)
                return row
