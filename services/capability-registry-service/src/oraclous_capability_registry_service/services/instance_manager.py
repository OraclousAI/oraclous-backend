"""Tool instance use-cases (ORAA-4 §21 services layer; reshape of legacy
``oraclous-core-service/app/services/instance_manager.py``).

Creates and configures tool instances. On create it resolves the instance's required credential
types from the tool descriptor and sets the lifecycle status accordingly (``READY`` when nothing is
required, else ``CONFIGURATION_REQUIRED``). Configuring credential mappings re-derives the status.
Every call carries the org + user from the authenticated principal (ORG001).
"""

from __future__ import annotations

import uuid

from oraclous_capability_registry_service.domain.errors import (
    CapabilityNotFoundError,
    InvalidDescriptorError,
)
from oraclous_capability_registry_service.domain.manifest import required_credential_types
from oraclous_capability_registry_service.models.enums import DescriptorKind, InstanceStatus
from oraclous_capability_registry_service.models.tool_instance import ToolInstance
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityRepository,
)
from oraclous_capability_registry_service.repositories.instance_repository import InstanceRepository
from oraclous_capability_registry_service.schema.instance_schema import (
    ConfigureCredentials,
    CreateInstance,
    InstanceOut,
)


class InstanceNotFoundError(Exception):
    """Instance does not exist in the caller's org — maps to HTTP 404 (mask)."""


def _out(row: ToolInstance) -> InstanceOut:
    return InstanceOut.model_validate(row)


def _status_for(required: list[str], mappings: dict[str, str]) -> InstanceStatus:
    missing = [c for c in required if c not in mappings]
    return InstanceStatus.CONFIGURATION_REQUIRED if missing else InstanceStatus.READY


class InstanceManager:
    def __init__(
        self, *, instances: InstanceRepository, capabilities: CapabilityRepository
    ) -> None:
        self._instances = instances
        self._capabilities = capabilities

    async def create(
        self, *, body: CreateInstance, organisation_id: uuid.UUID, user_id: uuid.UUID
    ) -> InstanceOut:
        descriptor = await self._capabilities.get_by_id(body.capability_id, organisation_id)
        if descriptor is None:
            raise CapabilityNotFoundError("capability not found")
        if DescriptorKind(descriptor.kind) is not DescriptorKind.TOOL:
            raise InvalidDescriptorError("instances can only be created for tool capabilities")
        required = required_credential_types(descriptor.descriptor)
        status = _status_for(required, {})
        row = await self._instances.create(
            organisation_id=organisation_id,
            capability_id=body.capability_id,
            user_id=user_id,
            name=body.name,
            description=body.description,
            configuration=body.configuration,
            settings=body.settings,
            required_credentials=required,
            status=status,
        )
        return _out(row)

    async def get(self, *, instance_id: uuid.UUID, organisation_id: uuid.UUID) -> InstanceOut:
        row = await self._instances.get_by_id(instance_id, organisation_id)
        if row is None:
            raise InstanceNotFoundError("instance not found")
        return _out(row)

    async def list(self, *, organisation_id: uuid.UUID) -> list[InstanceOut]:
        return [_out(r) for r in await self._instances.list_by_org(organisation_id)]

    async def configure_credentials(
        self, *, instance_id: uuid.UUID, body: ConfigureCredentials, organisation_id: uuid.UUID
    ) -> InstanceOut:
        existing = await self._instances.get_by_id(instance_id, organisation_id)
        if existing is None:
            raise InstanceNotFoundError("instance not found")
        status = _status_for(list(existing.required_credentials or []), body.credential_mappings)
        row = await self._instances.set_credentials_and_status(
            instance_id, organisation_id, body.credential_mappings, status
        )
        if row is None:
            raise InstanceNotFoundError("instance not found")
        return _out(row)
