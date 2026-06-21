"""AdoptedToolRun ORM model (models layer).

The idempotency ledger for an ADOPTED_TOOL_RUN schedule fire (#489). A schedule whose
``target_kind == adopted_tool_run`` fires a capability-registry instance ``/execute`` instead of a
durable harness engine_job — but the registry ``/execute`` is NOT itself idempotent on the firing
window, so this table is the dedupe gate: the ``(organisation_id, idempotency_key)`` unique
constraint (key = ``schedule_id:window``) mirrors the ``engine_jobs`` constraint that gives the
harness path its at-least-once-without-duplicates firing. The row is written transactionally BEFORE
any registry dispatch is enqueued, so a duplicate same-window fire (a Beat re-tick or a second
fire-now) hits the unique violation, returns None, and NO second execution is dispatched.

``execution_id`` is the registry ``ExecutionOut.id`` stamped AFTER the worker dispatches (nullable
until then). Org-scoped (ADR-006); the org-GUC backstop (ADR-030) is enabled on this table in
migration 0009 (same migration as the create — never deferred).

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_execution_engine_service.models.base_model import BaseModel


class AdoptedToolRun(BaseModel):
    __tablename__ = "engine_adopted_tool_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    schedule_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # the at-least-once dedupe key: ``schedule_id:window`` — unique within an org (uq below).
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # the registry ExecutionOut.id, stamped by the worker AFTER it dispatches (nullable until then).
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
