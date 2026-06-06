"""HarnessExecution ORM model (ORAA-4 §21 models layer).

One row per harness run: the resolved harness identity, the terminal status, the final output, and a
JSON step trace. Org-scoped (ADR-006). The registry keeps its own per-tool execution rows; this row
is the harness-level record and cross-references them through the provenance trail.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Column, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from oraclous_harness_runtime_service.models.base_model import BaseModel


class HarnessExecution(BaseModel):
    __tablename__ = "harness_executions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    harness_id = Column(UUID(as_uuid=True), nullable=False)
    harness_name = Column(String(255), nullable=False)
    content_hash = Column(String(64), nullable=True)
    status = Column(String(32), nullable=False)
    input = Column(Text, nullable=False)
    output = Column(Text, nullable=True)
    error_type = Column(String(128), nullable=True)
    error_message = Column(Text, nullable=True)
    iterations = Column(Integer, nullable=False, default=0)
    steps = Column(JSONB, nullable=False, default=list)
