import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capability_descriptor import CapabilityDescriptorDB, DescriptorKind
from ohm.hashing import compute_content_hash


class CapabilityDescriptorRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        org_id: uuid.UUID,
        kind: DescriptorKind,
        descriptor: Dict[str, Any],
        content_hash: Optional[str] = None,
    ) -> CapabilityDescriptorDB:
        row = CapabilityDescriptorDB(
            org_id=org_id,
            kind=kind,
            descriptor=descriptor,
            content_hash=compute_content_hash(descriptor),
        )
        self.db.add(row)
        await self.db.flush()
        await self.db.refresh(row)
        return row

    async def get_by_id(self, id: uuid.UUID) -> Optional[CapabilityDescriptorDB]:
        result = await self.db.execute(
            select(CapabilityDescriptorDB).where(CapabilityDescriptorDB.id == id)
        )
        return result.scalar_one_or_none()

    async def update_descriptor(
        self, id: uuid.UUID, descriptor: Dict[str, Any]
    ) -> Optional[CapabilityDescriptorDB]:
        row = await self.get_by_id(id)
        if row is None:
            return None
        row.descriptor = descriptor
        row.content_hash = compute_content_hash(descriptor)
        await self.db.flush()
        await self.db.refresh(row)
        return row

    async def delete(self, id: uuid.UUID) -> bool:
        result = await self.db.execute(
            delete(CapabilityDescriptorDB).where(CapabilityDescriptorDB.id == id)
        )
        return result.rowcount > 0

    async def list_by_org(self, org_id: uuid.UUID) -> List[CapabilityDescriptorDB]:
        result = await self.db.execute(
            select(CapabilityDescriptorDB).where(
                CapabilityDescriptorDB.org_id == org_id
            )
        )
        return list(result.scalars().all())

    async def list_by_kind(
        self, org_id: uuid.UUID, kind: DescriptorKind
    ) -> List[CapabilityDescriptorDB]:
        result = await self.db.execute(
            select(CapabilityDescriptorDB).where(
                CapabilityDescriptorDB.org_id == org_id,
                CapabilityDescriptorDB.kind == kind,
            )
        )
        return list(result.scalars().all())

    async def search_by_descriptor(
        self, org_id: uuid.UUID, filter_dict: Dict[str, Any]
    ) -> List[CapabilityDescriptorDB]:
        result = await self.db.execute(
            select(CapabilityDescriptorDB).where(
                CapabilityDescriptorDB.org_id == org_id,
                CapabilityDescriptorDB.descriptor.contains(filter_dict),
            )
        )
        return list(result.scalars().all())
