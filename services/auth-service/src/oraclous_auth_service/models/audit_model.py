"""Append-only auth audit log (R3.5-P3-S6).

Records security-relevant identity events (register / login / oauth-login / invitation-accept). Org-
scoped (``organisation_id`` nullable — a pre-org event has none). ``event_metadata`` (SQLAlchemy
reserves the bare ``metadata`` name) holds optional structured context.

No ``from __future__ import annotations`` — SQLAlchemy resolves ``Mapped[...]`` at mapper config.
"""

from datetime import datetime

from sqlalchemy import JSON, TIMESTAMP, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_auth_service.models.base import Base


class AuthAuditLog(Base):
    """One immutable audit record for an identity event."""

    __tablename__ = "auth_audit_log"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    organisation_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    actor_type: Mapped[str] = mapped_column(String(24), nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    event_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_auth_audit_log_event", "event"),)
