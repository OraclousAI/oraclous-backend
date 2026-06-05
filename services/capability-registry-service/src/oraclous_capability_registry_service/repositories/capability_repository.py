"""Capability descriptor repository (ORAA-4 §21 repositories layer; reshape of legacy
``oraclous-core-service/app/repositories/capability_descriptor_repository.py``).

The ONLY place that touches the DB driver for capability descriptors. Every read and write is
scoped by ``organisation_id`` (ADR-006) — supplied by the caller from the authenticated principal,
never a request body (ORG001). Writes auto-compute ``content_hash`` (canonical SHA-256) and the
denormalised ``name`` unless the caller passes an explicit hash.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_capability_registry_service.domain.hashing import compute_content_hash
from oraclous_capability_registry_service.domain.manifest import descriptor_name
from oraclous_capability_registry_service.models.base_model import Base
from oraclous_capability_registry_service.models.capability_descriptor import CapabilityDescriptor
from oraclous_capability_registry_service.models.enums import DescriptorKind

# Sentinel: "caller omitted content_hash" (auto-compute) vs "caller passed a value" (store as-is).
_AUTO: Any = object()


class CapabilityRepository:
    def __init__(self, db_url: str) -> None:
        self._engine = create_async_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def create_tables(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(
        self,
        *,
        organisation_id: uuid.UUID,
        kind: DescriptorKind,
        descriptor: dict[str, Any],
        descriptor_id: uuid.UUID | None = None,
        content_hash: str | None = _AUTO,
    ) -> CapabilityDescriptor:
        row = CapabilityDescriptor(
            id=descriptor_id or uuid.uuid4(),
            organisation_id=organisation_id,
            kind=kind,
            name=descriptor_name(descriptor),
            descriptor=descriptor,
            content_hash=(
                compute_content_hash(descriptor) if content_hash is _AUTO else content_hash
            ),
        )
        async with self._session() as session:
            async with session.begin():
                session.add(row)
            await session.refresh(row)
            return row

    async def upsert_by_id(
        self,
        *,
        organisation_id: uuid.UUID,
        descriptor_id: uuid.UUID,
        kind: DescriptorKind,
        descriptor: dict[str, Any],
    ) -> tuple[CapabilityDescriptor, str]:
        """Idempotently sync a descriptor by (id, org). Status is created|updated|unchanged.

        Used by plugin discovery: re-seeding the same descriptor is a no-op; a changed manifest
        (different content_hash) updates in place; a new descriptor is created.
        """
        new_hash = compute_content_hash(descriptor)
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(CapabilityDescriptor).where(
                        CapabilityDescriptor.id == descriptor_id,
                        CapabilityDescriptor.organisation_id == organisation_id,
                    )
                )
                row = result.scalars().first()
                if row is None:
                    row = CapabilityDescriptor(
                        id=descriptor_id,
                        organisation_id=organisation_id,
                        kind=kind,
                        name=descriptor_name(descriptor),
                        descriptor=descriptor,
                        content_hash=new_hash,
                    )
                    session.add(row)
                    status = "created"
                elif row.content_hash != new_hash:
                    row.descriptor = descriptor
                    row.name = descriptor_name(descriptor)
                    row.content_hash = new_hash
                    status = "updated"
                else:
                    status = "unchanged"
            await session.refresh(row)
            return row, status

    async def get_by_id(
        self, descriptor_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> CapabilityDescriptor | None:
        async with self._session() as session:
            result = await session.execute(
                select(CapabilityDescriptor).where(
                    CapabilityDescriptor.id == descriptor_id,
                    CapabilityDescriptor.organisation_id == organisation_id,
                )
            )
            return result.scalars().first()

    async def list_by_org(self, organisation_id: uuid.UUID) -> list[CapabilityDescriptor]:
        async with self._session() as session:
            result = await session.execute(
                select(CapabilityDescriptor)
                .where(CapabilityDescriptor.organisation_id == organisation_id)
                .order_by(CapabilityDescriptor.created_at)
            )
            return list(result.scalars().all())

    async def list_by_kind(
        self, organisation_id: uuid.UUID, kind: DescriptorKind
    ) -> list[CapabilityDescriptor]:
        async with self._session() as session:
            result = await session.execute(
                select(CapabilityDescriptor)
                .where(
                    CapabilityDescriptor.organisation_id == organisation_id,
                    CapabilityDescriptor.kind == kind,
                )
                .order_by(CapabilityDescriptor.created_at)
            )
            return list(result.scalars().all())

    async def search_by_descriptor(
        self, organisation_id: uuid.UUID, filter_dict: dict[str, Any]
    ) -> list[CapabilityDescriptor]:
        """Org-scoped JSONB containment (``descriptor @> filter_dict``)."""
        async with self._session() as session:
            result = await session.execute(
                select(CapabilityDescriptor).where(
                    CapabilityDescriptor.organisation_id == organisation_id,
                    CapabilityDescriptor.descriptor.contains(filter_dict),
                )
            )
            return list(result.scalars().all())

    async def match_capabilities(
        self, organisation_id: uuid.UUID, capability_names: list[str]
    ) -> list[CapabilityDescriptor]:
        """Tool descriptors whose ``spec.capabilities`` includes any of ``capability_names``.

        Uses JSONB containment per name (``descriptor @> {"spec":{"capabilities":[{"name":n}]}}``),
        unioned by descriptor id with first-seen order preserved (deterministic).
        """
        seen: dict[uuid.UUID, CapabilityDescriptor] = {}
        for name in capability_names:
            rows = await self.search_by_descriptor(
                organisation_id, {"spec": {"capabilities": [{"name": name}]}}
            )
            for row in rows:
                seen.setdefault(row.id, row)
        return list(seen.values())

    async def update_descriptor(
        self, descriptor_id: uuid.UUID, organisation_id: uuid.UUID, descriptor: dict[str, Any]
    ) -> CapabilityDescriptor | None:
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(CapabilityDescriptor).where(
                        CapabilityDescriptor.id == descriptor_id,
                        CapabilityDescriptor.organisation_id == organisation_id,
                    )
                )
                row = result.scalars().first()
                if row is None:
                    return None
                row.descriptor = descriptor
                row.name = descriptor_name(descriptor)
                row.content_hash = compute_content_hash(descriptor)
            # Reload the server-side onupdate `updated_at` before the session closes (else the
            # detached row triggers a refresh on attribute access — DetachedInstanceError).
            await session.refresh(row)
            return row

    async def delete(self, descriptor_id: uuid.UUID, organisation_id: uuid.UUID) -> bool:
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(CapabilityDescriptor).where(
                        CapabilityDescriptor.id == descriptor_id,
                        CapabilityDescriptor.organisation_id == organisation_id,
                    )
                )
                row = result.scalars().first()
                if row is None:
                    return False
                await session.delete(row)
            return True
