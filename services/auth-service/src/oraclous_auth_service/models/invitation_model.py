"""ORM model for organisation invitations (R3.5-P3-S3, threat T-INVITE).

Org-scoped (``organisation_id``). The raw token is never stored — only ``token_hash`` (SHA-256) and
an indexed ``token_prefix`` for lookup. At most one ``pending`` invitation per (org, email): a new
one supersedes the prior pending row.

No ``from __future__ import annotations`` — SQLAlchemy resolves ``Mapped[...]`` at mapper config.
"""

from datetime import datetime

from sqlalchemy import JSON, TIMESTAMP, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_auth_service.models.base import Base


class OrgInvitation(Base):
    """A pending/accepted/revoked/expired invitation of an email to an org with a role."""

    __tablename__ = "org_invitations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    organisation_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    org_role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    token_hash: Mapped[str] = mapped_column(String, nullable=False)
    token_prefix: Mapped[str] = mapped_column(String, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    subgraph_grants: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    invited_by_user_id: Mapped[str] = mapped_column(String, nullable=False)
    accepted_by_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (Index("ix_org_invitations_org_email", "organisation_id", "email"),)
