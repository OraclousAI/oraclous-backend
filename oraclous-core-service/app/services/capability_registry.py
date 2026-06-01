import logging
import uuid
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from app.models.capability_descriptor import CapabilityDescriptorDB, DescriptorKind
from app.repositories.capability_descriptor_repository import CapabilityDescriptorRepository

if TYPE_CHECKING:
    from app.schemas.tool_definition import ToolDefinition


def _capability_to_tool_definition(capability: CapabilityDescriptorDB) -> "ToolDefinition":
    """Convert a CapabilityDescriptorDB (OHM descriptor) to a ToolDefinition schema object."""
    from app.schemas.tool_definition import (
        ToolDefinition,
        ToolCapability,
        CredentialRequirement,
        ToolSchema,
    )
    from app.schemas.common import ToolCategory, ToolType, CredentialType

    d = capability.descriptor
    meta = d.get("metadata", {})
    spec = d.get("spec", {})
    version_info = d.get("version", {})

    tags = version_info.get("tags", [])
    version_str = tags[0] if tags else "1.0.0"

    cred_reqs: List[CredentialRequirement] = []
    for cr in spec.get("credential_requirements", []):
        try:
            cred_reqs.append(
                CredentialRequirement(
                    type=CredentialType(cr["type"]),
                    required=cr.get("required", True),
                    provider=cr.get("provider", cr.get("type", "unknown")),
                    scopes=cr.get("scopes"),
                    description=cr.get("description"),
                )
            )
        except Exception as e:
            logger.warning("Skipping malformed credential_requirement entry for capability %s: %s", capability.id, e)

    capabilities: List[ToolCapability] = []
    for cap in spec.get("capabilities", []):
        try:
            capabilities.append(ToolCapability(**cap))
        except Exception as e:
            logger.warning("Skipping malformed capability entry for capability %s: %s", capability.id, e)

    def _schema(data: Any) -> ToolSchema:
        if isinstance(data, dict):
            try:
                return ToolSchema(**data)
            except Exception as e:
                logger.warning("Malformed schema dict for capability %s, using empty object: %s", capability.id, e)
        return ToolSchema(type="object")

    raw_category = spec.get("category", "INGESTION")
    try:
        category = ToolCategory(raw_category)
    except ValueError:
        category = ToolCategory.INGESTION

    raw_type = spec.get("type", "INTERNAL")
    try:
        tool_type = ToolType(raw_type)
    except ValueError:
        tool_type = ToolType.INTERNAL

    config_data = spec.get("configuration_schema")
    config_schema = _schema(config_data) if config_data else None

    return ToolDefinition(
        id=capability.id,
        name=meta.get("name", d.get("id", str(capability.id))),
        description=meta.get("description", ""),
        version=version_str,
        category=category,
        type=tool_type,
        capabilities=capabilities,
        input_schema=_schema(spec.get("input_schema")),
        output_schema=_schema(spec.get("output_schema")),
        configuration_schema=config_schema,
        credential_requirements=cred_reqs,
    )


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

    async def get_tool_definition(self, id: uuid.UUID) -> Optional["ToolDefinition"]:
        """Look up a capability descriptor by UUID and return as ToolDefinition."""
        capability = await self.get_by_id(id)
        if capability is None:
            return None
        return _capability_to_tool_definition(capability)

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
