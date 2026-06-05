"""Tool instance repository (ORAA-4 §21 repositories layer; reshape of legacy
``oraclous-core-service/app/repositories/instance_repository.py``).

The only DB seam for tool instances. Every read/write is org-scoped (ADR-006) — the organisation is
supplied by the caller from the authenticated principal, never a request body (ORG001).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_capability_registry_service.models.enums import InstanceStatus
from oraclous_capability_registry_service.models.tool_instance import ToolInstance


class InstanceRepository:
    def __init__(self, db_url: str) -> None:
        self._engine = create_async_engine(db_url, echo=False)
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
        async with self._session() as session:
            async with session.begin():
                session.add(row)
            await session.refresh(row)
            return row

    async def get_by_id(
        self, instance_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> ToolInstance | None:
        async with self._session() as session:
            result = await session.execute(
                select(ToolInstance).where(
                    ToolInstance.id == instance_id,
                    ToolInstance.organisation_id == organisation_id,
                )
            )
            return result.scalars().first()

    async def list_by_org(self, organisation_id: uuid.UUID) -> list[ToolInstance]:
        async with self._session() as session:
            result = await session.execute(
                select(ToolInstance)
                .where(ToolInstance.organisation_id == organisation_id)
                .order_by(ToolInstance.created_at)
            )
            return list(result.scalars().all())

    async def set_credentials_and_status(
        self,
        instance_id: uuid.UUID,
        organisation_id: uuid.UUID,
        credential_mappings: dict[str, str],
        status: InstanceStatus,
    ) -> ToolInstance | None:
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
