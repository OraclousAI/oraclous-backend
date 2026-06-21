"""ToolInstance ORM (models layer; reshape of legacy
``oraclous-core-service/app/models/tool_instance.py``).

A configured instance of a tool bound to an org + user. Org-scoped (``organisation_id`` NOT NULL —
ADR-006/ORG002); ``capability_id`` references the registry descriptor (a kind=tool capability), not
a separate tool_definitions table. The legacy ``workflow_id`` FK is dropped (workflows retired,
ADR-005).

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_capability_registry_service.models.base_model import BaseModel
from oraclous_capability_registry_service.models.enums import InstanceStatus


class ToolInstance(BaseModel):
    __tablename__ = "tool_instances"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    capability_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("capability_descriptors.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    credential_mappings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    required_credentials: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[InstanceStatus] = mapped_column(
        SAEnum(
            InstanceStatus,
            name="instancestatus",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=InstanceStatus.PENDING,
        index=True,
    )
    last_execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    execution_count: Mapped[Decimal] = mapped_column(Numeric(10, 0), nullable=False, default=0)
    total_credits_consumed: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=0
    )
