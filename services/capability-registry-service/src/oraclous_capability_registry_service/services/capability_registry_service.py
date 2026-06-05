"""Capability registry use-cases (ORAA-4 §21 services layer; reshape of legacy
``oraclous-core-service/app/services/capability_registry.py``).

The sole authority for capability lookups. Orchestrates the org-scoped repository with OHM-v1
validation: ``create`` validates the descriptor before persisting (a malformed descriptor never
reaches the table) and the repository auto-computes ``content_hash``. Every call carries the
``organisation_id`` from the authenticated principal (ORG001 — never the request body).
"""

from __future__ import annotations

import uuid
from typing import Any

from oraclous_capability_registry_service.domain.errors import CapabilityNotFoundError
from oraclous_capability_registry_service.domain.manifest import validate_descriptor
from oraclous_capability_registry_service.models.capability_descriptor import CapabilityDescriptor
from oraclous_capability_registry_service.models.enums import DescriptorKind
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityRepository,
)
from oraclous_capability_registry_service.schema.capability_schema import (
    CapabilityOut,
    CreateCapability,
    UpdateCapability,
)


def _out(row: CapabilityDescriptor) -> CapabilityOut:
    return CapabilityOut.model_validate(row)


class CapabilityRegistryService:
    def __init__(self, *, repository: CapabilityRepository) -> None:
        self._repo = repository

    async def create(self, *, body: CreateCapability, organisation_id: uuid.UUID) -> CapabilityOut:
        validate_descriptor(body.kind, body.descriptor)
        row = await self._repo.create(
            organisation_id=organisation_id,
            kind=body.kind,
            descriptor=body.descriptor,
            descriptor_id=body.descriptor_id,
        )
        return _out(row)

    async def get(self, *, capability_id: uuid.UUID, organisation_id: uuid.UUID) -> CapabilityOut:
        row = await self._repo.get_by_id(capability_id, organisation_id)
        if row is None:
            raise CapabilityNotFoundError("capability not found")
        return _out(row)

    async def list(
        self, *, organisation_id: uuid.UUID, kind: DescriptorKind | None = None
    ) -> list[CapabilityOut]:
        rows = (
            await self._repo.list_by_kind(organisation_id, kind)
            if kind is not None
            else await self._repo.list_by_org(organisation_id)
        )
        return [_out(r) for r in rows]

    async def search(
        self, *, organisation_id: uuid.UUID, filter_dict: dict[str, Any]
    ) -> list[CapabilityOut]:
        rows = await self._repo.search_by_descriptor(organisation_id, filter_dict)
        return [_out(r) for r in rows]

    async def match_capabilities(
        self, *, organisation_id: uuid.UUID, capability_names: list[str]
    ) -> list[CapabilityOut]:
        rows = await self._repo.match_capabilities(organisation_id, capability_names)
        return [_out(r) for r in rows]

    async def update(
        self, *, capability_id: uuid.UUID, body: UpdateCapability, organisation_id: uuid.UUID
    ) -> CapabilityOut:
        existing = await self._repo.get_by_id(capability_id, organisation_id)
        if existing is None:
            raise CapabilityNotFoundError("capability not found")
        validate_descriptor(DescriptorKind(existing.kind), body.descriptor)
        row = await self._repo.update_descriptor(capability_id, organisation_id, body.descriptor)
        if row is None:
            raise CapabilityNotFoundError("capability not found")
        return _out(row)

    async def delete(self, *, capability_id: uuid.UUID, organisation_id: uuid.UUID) -> None:
        if not await self._repo.delete(capability_id, organisation_id):
            raise CapabilityNotFoundError("capability not found")
