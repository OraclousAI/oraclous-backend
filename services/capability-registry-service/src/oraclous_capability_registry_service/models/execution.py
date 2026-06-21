"""Execution ORM (models layer; reshape of legacy
``oraclous-core-service/app/models/execution.py``).

Provenance of every tool dispatch. Org-scoped (ADR-006/ORG002). ``credential_refs`` records which
credential types/scopes were used for audit lineage — **never the secret material** itself. The
legacy ``workflow_id`` and async ``jobs`` are dropped (ADR-005; async execution → R5).

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_capability_registry_service.models.base_model import BaseModel
from oraclous_capability_registry_service.models.enums import ExecutionStatus


class Execution(BaseModel):
    __tablename__ = "executions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    capability_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[ExecutionStatus] = mapped_column(
        SAEnum(
            ExecutionStatus,
            name="executionstatus",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        index=True,
    )
    input_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    credential_refs: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )  # types/scopes used — NEVER the secret
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    credits_consumed: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=0)
    processing_time_ms: Mapped[Decimal | None] = mapped_column(Numeric(10, 0), nullable=True)
