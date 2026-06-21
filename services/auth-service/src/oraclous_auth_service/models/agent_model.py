"""ORM models for the agent principal and its credentials (R1-A1).

Reshaped from the legacy service-account-key pattern
(``auth-service/app/models/service_account_model.AgentServiceAccountKey``) onto a
first-class ``agent`` principal. The raw credential is NEVER persisted — only its
bcrypt hash and a prefix index. ``organisation_id`` is carried on both the
principal and its credentials per ADR-006 (legacy held it on the credential as
``tenant_id``).

Note: no ``from __future__ import annotations`` here — SQLAlchemy resolves the
``Mapped[...]`` annotations at mapper configuration, so they must be real types.
"""

from datetime import datetime

from sqlalchemy import TIMESTAMP, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_auth_service.models.base import Base


class Agent(Base):
    """An agent principal, scoped to the organisation it is created in."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    organisation_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class AgentCredential(Base):
    """A bcrypt-hashed, prefix-indexed credential for an :class:`Agent`.

    The raw credential exists only in the issuing call's return value; this
    record stores the hash and the lookup prefix, never the secret.
    """

    __tablename__ = "agent_credentials"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    organisation_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    # Which machine-principal type this credential mints: "agent" (default) or "service_account".
    # server_default mirrors migration 0004 so raw inserts (org-isolation tests) default too.
    principal_type: Mapped[str] = mapped_column(
        String, nullable=False, default="agent", server_default=text("'agent'")
    )
    credential_hash: Mapped[str] = mapped_column(String, nullable=False)
    credential_prefix: Mapped[str] = mapped_column(String, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    # ADR-012 §1a (a): a prefix maps to at most one *active* principal, ever —
    # pinning the invariant at the schema layer keeps the
    # ``active_credentials_by_prefix`` lookup from becoming a cross-org
    # enumeration surface, regardless of how the application code is wired.
    __table_args__ = (
        Index(
            "ix_agent_credentials_active_prefix_unique",
            "credential_prefix",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )
