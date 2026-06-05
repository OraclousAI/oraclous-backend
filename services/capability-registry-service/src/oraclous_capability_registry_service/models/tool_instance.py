"""ToolInstance ORM (ORAA-4 §21 models layer; reshape of legacy
``oraclous-core-service/app/models/tool_instance.py``).

A configured instance of a tool bound to an org + user. Org-scoped (``organisation_id`` NOT NULL —
ADR-006/ORG002); ``capability_id`` references the registry descriptor (a kind=tool capability), not
a separate tool_definitions table. The legacy ``workflow_id`` FK is dropped (workflows retired,
ADR-005).
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Numeric, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID

from oraclous_capability_registry_service.models.base_model import BaseModel
from oraclous_capability_registry_service.models.enums import InstanceStatus


class ToolInstance(BaseModel):
    __tablename__ = "tool_instances"

    id = Column(UUID(as_uuid=True), primary_key=True)
    organisation_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    capability_id = Column(
        UUID(as_uuid=True),
        ForeignKey("capability_descriptors.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    configuration = Column(JSONB, nullable=False, default=dict)
    settings = Column(JSONB, nullable=False, default=dict)
    credential_mappings = Column(JSONB, nullable=False, default=dict)
    required_credentials = Column(JSONB, nullable=False, default=list)
    status = Column(
        SAEnum(
            InstanceStatus,
            name="instancestatus",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=InstanceStatus.PENDING,
        index=True,
    )
    last_execution_id = Column(UUID(as_uuid=True), nullable=True)
    execution_count = Column(Numeric(10, 0), nullable=False, default=0)
    total_credits_consumed = Column(Numeric(10, 4), nullable=False, default=0)
