"""ORM models package — importing it registers all tables on ``Base.metadata`` (for Alembic)."""

from __future__ import annotations

from oraclous_capability_registry_service.models.base_model import Base, BaseModel
from oraclous_capability_registry_service.models.capability_descriptor import CapabilityDescriptor
from oraclous_capability_registry_service.models.enums import DescriptorKind, InstanceStatus
from oraclous_capability_registry_service.models.tool_instance import ToolInstance

__all__ = [
    "Base",
    "BaseModel",
    "CapabilityDescriptor",
    "DescriptorKind",
    "InstanceStatus",
    "ToolInstance",
]
