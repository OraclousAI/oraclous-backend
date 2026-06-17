"""HarnessAssignment ORM model (ORAA-4 §21 models layer).

A human-actor task-board assignment: when a harness's entrypoint actor is a human, the runtime halts
the run (escalation) and records the work to be done here, addressed to a workspace ``human_role``.
R4 creates the assignment (status ``PENDING``); the durable claim/complete round-trip is R5
(execution-engine). Org-scoped (ADR-006).

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_harness_runtime_service.models.base_model import BaseModel


class HarnessAssignment(BaseModel):
    __tablename__ = "harness_assignments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    harness_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    human_role: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    input: Mapped[str] = mapped_column(Text, nullable=False)
