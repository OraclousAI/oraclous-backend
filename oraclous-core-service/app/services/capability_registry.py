import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capability_descriptor import CapabilityDescriptorDB, DescriptorKind
from app.repositories.capability_descriptor_repository import CapabilityDescriptorRepository


class CapabilityRegistryService:
    def __init__(self, db: AsyncSession):
        self._repo = CapabilityDescriptorRepository(db)

    async def create(
        self,
        org_id: uuid.UUID,
        kind: DescriptorKind,
        descriptor: Dict[str, Any],
        content_hash: Optional[str] = None,
    ) -> CapabilityDescriptorDB:
        return await self._repo.create(
            org_id=org_id,
            kind=kind,
            descriptor=descriptor,
            content_hash=content_hash,
        )

    async def get_by_id(self, id: uuid.UUID) -> Optional[CapabilityDescriptorDB]:
        return await self._repo.get_by_id(id)

    async def update(
        self, id: uuid.UUID, descriptor: Dict[str, Any]
    ) -> Optional[CapabilityDescriptorDB]:
        return await self._repo.update_descriptor(id, descriptor)

    async def delete(self, id: uuid.UUID) -> bool:
        return await self._repo.delete(id)

    async def list_by_org(self, org_id: uuid.UUID) -> List[CapabilityDescriptorDB]:
        return await self._repo.list_by_org(org_id)

    async def list_by_kind(
        self, org_id: uuid.UUID, kind: DescriptorKind
    ) -> List[CapabilityDescriptorDB]:
        return await self._repo.list_by_kind(org_id, kind)

    async def search_by_descriptor(
        self, org_id: uuid.UUID, filter_dict: Dict[str, Any]
    ) -> List[CapabilityDescriptorDB]:
        return await self._repo.search_by_descriptor(org_id, filter_dict)
