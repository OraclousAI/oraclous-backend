from __future__ import annotations

import uuid
from typing import Any

from app.models.capability_descriptor import CapabilityDescriptorDB, DescriptorKind
from app.repositories.capability_descriptor_repository import CapabilityDescriptorRepository
from sqlalchemy.ext.asyncio import AsyncSession


class CapabilityRegistryService:
    """Single DB-backed capability registry — the sole authority for all capability lookups.

    Wraps CapabilityDescriptorRepository. All CRUD and query operations route through
    this class; no in-memory registry or sync service exists alongside it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._repo = CapabilityDescriptorRepository(session)

    async def create(
        self,
        org_id: uuid.UUID,
        kind: DescriptorKind,
        descriptor: dict[str, Any],
        content_hash: str | None = None,
    ) -> CapabilityDescriptorDB:
        """Persist a new capability descriptor. Stores content_hash as-is (None when omitted)."""
        return await self._repo.create(
            org_id=org_id, kind=kind, descriptor=descriptor, content_hash=content_hash
        )

    async def get_by_id(self, id: uuid.UUID) -> CapabilityDescriptorDB | None:
        return await self._repo.get_by_id(id)

    async def update(
        self, id: uuid.UUID, descriptor: dict[str, Any]
    ) -> CapabilityDescriptorDB | None:
        return await self._repo.update_descriptor(id, descriptor)

    async def delete(self, id: uuid.UUID) -> bool:
        return await self._repo.delete(id)

    async def list_by_org(self, org_id: uuid.UUID) -> list[CapabilityDescriptorDB]:
        return await self._repo.list_by_org(org_id)

    async def list_by_kind(
        self, org_id: uuid.UUID, kind: DescriptorKind
    ) -> list[CapabilityDescriptorDB]:
        return await self._repo.list_by_kind(org_id, kind)

    async def search_by_descriptor(
        self, org_id: uuid.UUID, filter_dict: dict[str, Any]
    ) -> list[CapabilityDescriptorDB]:
        return await self._repo.search_by_descriptor(org_id, filter_dict)
