import uuid
from typing import Any

from app.models.capability_descriptor import CapabilityDescriptorDB, DescriptorKind
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession


class CapabilityDescriptorRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        org_id: uuid.UUID,
        kind: DescriptorKind,
        descriptor: dict[str, Any],
        content_hash: str | None = None,
    ) -> CapabilityDescriptorDB:
        row = CapabilityDescriptorDB(
            org_id=org_id,
            kind=kind,
            descriptor=descriptor,
            content_hash=content_hash,
        )
        self.db.add(row)
        await self.db.flush()
        await self.db.refresh(row)
        return row

    async def get_by_id(self, id: uuid.UUID) -> CapabilityDescriptorDB | None:
        result = await self.db.execute(
            select(CapabilityDescriptorDB).where(CapabilityDescriptorDB.id == id)
        )
        return result.scalar_one_or_none()

    async def update_descriptor(
        self, id: uuid.UUID, descriptor: dict[str, Any]
    ) -> CapabilityDescriptorDB | None:
        row = await self.get_by_id(id)
        if row is None:
            return None
        row.descriptor = descriptor
        await self.db.flush()
        await self.db.refresh(row)
        return row

    async def delete(self, id: uuid.UUID) -> bool:
        result = await self.db.execute(
            delete(CapabilityDescriptorDB).where(CapabilityDescriptorDB.id == id)
        )
        return result.rowcount > 0

    async def list_by_org(self, org_id: uuid.UUID) -> list[CapabilityDescriptorDB]:
        result = await self.db.execute(
            select(CapabilityDescriptorDB).where(CapabilityDescriptorDB.org_id == org_id)
        )
        return list(result.scalars().all())

    async def list_by_kind(
        self, org_id: uuid.UUID, kind: DescriptorKind
    ) -> list[CapabilityDescriptorDB]:
        result = await self.db.execute(
            select(CapabilityDescriptorDB).where(
                CapabilityDescriptorDB.org_id == org_id,
                CapabilityDescriptorDB.kind == kind,
            )
        )
        return list(result.scalars().all())

    async def search_by_descriptor(
        self, org_id: uuid.UUID, filter_dict: dict[str, Any]
    ) -> list[CapabilityDescriptorDB]:
        result = await self.db.execute(
            select(CapabilityDescriptorDB).where(
                CapabilityDescriptorDB.org_id == org_id,
                CapabilityDescriptorDB.descriptor.contains(filter_dict),
            )
        )
        return list(result.scalars().all())
