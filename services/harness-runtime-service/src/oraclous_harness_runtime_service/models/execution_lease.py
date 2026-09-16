"""HarnessExecutionLease ORM model (models layer).

The cross-replica cancel lease (#1072, migration 0010): one row per in-flight execution, keyed by
the globally unique ``execution_id`` the engine mints per dispatch. Org-scoped (ADR-006), RLS-scoped
like the other four harness tables. ``cancel_requested_at`` starts NULL and is set once a caller
org-scoped ``request_cancel``s the run; the owning replica's watcher polls it. No ``updated_at`` —
the row is only ever created, flipped, read, or deleted outright.

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from oraclous_harness_runtime_service.models.base_model import Base


class HarnessExecutionLease(Base):
    __tablename__ = "harness_execution_leases"

    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
