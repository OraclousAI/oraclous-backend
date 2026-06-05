"""Capability descriptor repository (ORAA-4 §21 repositories layer; reshape of legacy
``oraclous-core-service/app/repositories/capability_descriptor_repository.py``).

The ONLY place that touches the DB driver for capability descriptors. Every read and write is
scoped by ``organisation_id`` (ADR-006) — supplied by the caller from the authenticated principal,
never a request body (ORG001). Writes auto-compute ``content_hash`` (canonical SHA-256) and the
denormalised ``name`` unless the caller passes an explicit hash.

Global/platform tools: when a ``platform_org_id`` is configured, *reads* are widened to also return
descriptors owned by that platform org (the built-in catalogue), so every tenant org sees the global
tools alongside its own. Results are de-duplicated by id preferring the caller-org row, so a tenant
custom tool with a colliding deterministic id shadows the platform built-in. *Writes* stay strict to
the caller org. When ``platform_org_id`` is None or equals the caller org, behaviour is exactly the
old single-org equality.
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
    def __init__(self, db_url: str, *, platform_org_id: uuid.UUID | None = None) -> None:
        self._engine = create_async_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)
        self._platform_org_id = platform_org_id

    def _read_org_filter(self, organisation_id: uuid.UUID) -> Any:
        """Org-scope predicate for *reads*. Widened to the platform org (global tools) when one is
        configured and distinct from the caller; otherwise strict caller-org equality."""
        if self._platform_org_id is not None and self._platform_org_id != organisation_id:
            return CapabilityDescriptor.organisation_id.in_(
                (organisation_id, self._platform_org_id)
            )
        return CapabilityDescriptor.organisation_id == organisation_id

    def _dedupe_prefer_caller(
        self, rows: list[CapabilityDescriptor], organisation_id: uuid.UUID
    ) -> list[CapabilityDescriptor]:
        """De-duplicate widened read results by id, preferring the caller-org row over the platform
        row (a tenant custom tool shadows a colliding built-in). First-seen order is preserved."""
        if self._platform_org_id is None or self._platform_org_id == organisation_id:
            return rows
        by_id: dict[uuid.UUID, CapabilityDescriptor] = {}
        for row in rows:
            existing = by_id.get(row.id)
            if existing is None:
                by_id[row.id] = row
            elif (
                existing.organisation_id == self._platform_org_id
                and row.organisation_id == organisation_id
            ):
                # Replace the platform row with the caller-org row (caller shadows the built-in).
                by_id[row.id] = row
        return list(by_id.values())

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
                    self._read_org_filter(organisation_id),
                )
            )
            rows = self._dedupe_prefer_caller(list(result.scalars().all()), organisation_id)
            return rows[0] if rows else None

    async def list_by_org(self, organisation_id: uuid.UUID) -> list[CapabilityDescriptor]:
        async with self._session() as session:
            result = await session.execute(
                select(CapabilityDescriptor)
                .where(self._read_org_filter(organisation_id))
                .order_by(CapabilityDescriptor.created_at)
            )
            return self._dedupe_prefer_caller(list(result.scalars().all()), organisation_id)

    async def list_by_kind(
        self, organisation_id: uuid.UUID, kind: DescriptorKind
    ) -> list[CapabilityDescriptor]:
        async with self._session() as session:
            result = await session.execute(
                select(CapabilityDescriptor)
                .where(
                    self._read_org_filter(organisation_id),
                    CapabilityDescriptor.kind == kind,
                )
                .order_by(CapabilityDescriptor.created_at)
            )
            return self._dedupe_prefer_caller(list(result.scalars().all()), organisation_id)

    async def search_by_descriptor(
        self, organisation_id: uuid.UUID, filter_dict: dict[str, Any]
    ) -> list[CapabilityDescriptor]:
        """Org-scoped JSONB containment (``descriptor @> filter_dict``); widened to the platform
        org (global tools) when configured."""
        async with self._session() as session:
            result = await session.execute(
                select(CapabilityDescriptor).where(
                    self._read_org_filter(organisation_id),
                    CapabilityDescriptor.descriptor.contains(filter_dict),
                )
            )
            return self._dedupe_prefer_caller(list(result.scalars().all()), organisation_id)

    async def match_capabilities(
        self, organisation_id: uuid.UUID, capability_names: list[str]
    ) -> list[CapabilityDescriptor]:
        """Tool descriptors whose ``spec.capabilities`` includes any of ``capability_names``.

        Uses JSONB containment per name (``descriptor @> {"spec":{"capabilities":[{"name":n}]}}``),
        unioned by descriptor id with first-seen order preserved (deterministic). Reads are widened
        to the platform org (global tools); the caller-org row shadows a colliding built-in id.
        """
        seen: dict[uuid.UUID, CapabilityDescriptor] = {}
        for name in capability_names:
            rows = await self.search_by_descriptor(
                organisation_id, {"spec": {"capabilities": [{"name": name}]}}
            )
            for row in rows:
                existing = seen.get(row.id)
                if existing is None:
                    seen[row.id] = row
                elif (
                    self._platform_org_id is not None
                    and existing.organisation_id == self._platform_org_id
                    and row.organisation_id == organisation_id
                ):
                    # Caller-org row shadows the platform built-in seen under an earlier name.
                    seen[row.id] = row
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
