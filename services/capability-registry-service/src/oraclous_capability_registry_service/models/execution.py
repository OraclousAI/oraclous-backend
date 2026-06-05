"""Execution ORM (ORAA-4 §21 models layer; reshape of legacy
``oraclous-core-service/app/models/execution.py``).

Provenance of every tool dispatch. Org-scoped (ADR-006/ORG002). ``credential_refs`` records which
credential types/scopes were used for audit lineage — **never the secret material** itself. The
legacy ``workflow_id`` and async ``jobs`` are dropped (ADR-005; async execution → R5).
"""

from __future__ import annotations

from sqlalchemy import Column, Numeric, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID

from oraclous_capability_registry_service.models.base_model import BaseModel
from oraclous_capability_registry_service.models.enums import ExecutionStatus


class Execution(BaseModel):
    __tablename__ = "executions"

    id = Column(UUID(as_uuid=True), primary_key=True)
    organisation_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    instance_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    capability_id = Column(UUID(as_uuid=True), nullable=False)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    agent_id = Column(UUID(as_uuid=True), nullable=True)
    status = Column(
        SAEnum(
            ExecutionStatus,
            name="executionstatus",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        index=True,
    )
    input_data = Column(JSONB, nullable=True)
    output_data = Column(JSONB, nullable=True)
    credential_refs = Column(JSONB, nullable=True)  # types/scopes used — NEVER the secret
    error_message = Column(Text, nullable=True)
    error_type = Column(String(100), nullable=True)
    credits_consumed = Column(Numeric(10, 4), nullable=False, default=0)
    processing_time_ms = Column(Numeric(10, 0), nullable=True)
