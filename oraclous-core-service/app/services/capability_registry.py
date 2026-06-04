from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capability_descriptor import CapabilityDescriptorDB, DescriptorKind
from app.repositories.capability_descriptor_repository import CapabilityDescriptorRepository

# Mirrors the repo sentinel: when omitted, the repo auto-computes the hash.
# When explicitly passed (including None), the caller's value is stored as-is.
_UNSET: Any = object()


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
        content_hash: str | None = _UNSET,
    ) -> CapabilityDescriptorDB:
        """Persist a new capability descriptor.

        When content_hash is omitted the repository auto-computes it from the
        descriptor payload.  When passed explicitly (including None) it is stored
        as-is, matching the repo's _UNSET sentinel contract.
        """
        kw: dict[str, Any] = dict(org_id=org_id, kind=kind, descriptor=descriptor)
        if content_hash is not _UNSET:
            kw["content_hash"] = content_hash
        return await self._repo.create(**kw)

    async def get_by_id(self, capability_id: uuid.UUID) -> CapabilityDescriptorDB | None:
        return await self._repo.get_by_id(capability_id)

    async def update(
        self, capability_id: uuid.UUID, descriptor: dict[str, Any]
    ) -> CapabilityDescriptorDB | None:
        return await self._repo.update_descriptor(capability_id, descriptor)

    async def delete(self, capability_id: uuid.UUID) -> bool:
        return await self._repo.delete(capability_id)

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
