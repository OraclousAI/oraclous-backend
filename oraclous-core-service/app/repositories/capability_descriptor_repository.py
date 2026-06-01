import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capability_descriptor import CapabilityDescriptorDB, DescriptorKind

# Sentinel that distinguishes "caller did not pass content_hash" from
# "caller explicitly passed content_hash=None".  When omitted, the hash is
# computed server-side.  When explicitly passed (including None), the caller-
# supplied value is stored as-is.
_UNSET: Any = object()


def _compute_content_hash(descriptor: Dict[str, Any]) -> str:
    canonical = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class CapabilityDescriptorRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        org_id: uuid.UUID,
        kind: DescriptorKind,
        descriptor: Dict[str, Any],
        content_hash: Optional[str] = _UNSET,
    ) -> CapabilityDescriptorDB:
        if content_hash is _UNSET:
            content_hash = _compute_content_hash(descriptor)
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
        row.content_hash = _compute_content_hash(descriptor)
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
