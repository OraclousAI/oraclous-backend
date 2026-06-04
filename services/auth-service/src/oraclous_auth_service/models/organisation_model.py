"""ORM models for organisations + membership (R3.5-P3-S2).

`organisations` is the tenancy scope-root (its ``id`` IS the organisation_id every other table is
scoped by), so it declares no ``organisation_id`` column of its own. `org_members` is the edge
(user ↔ org with a role) and backs the governance ``MembershipResolver`` — it IS org-scoped.

No ``from __future__ import annotations`` — SQLAlchemy resolves ``Mapped[...]`` at mapper config.
"""

from datetime import datetime

from sqlalchemy import JSON, TIMESTAMP, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_auth_service.models.base import Base


class Organisation(Base):
    """A tenant organisation. ``slug`` is a unique, immutable URL handle.

    org-scoping: scope-root — this table defines organisations; its ``id`` is the organisation_id
    that scopes every tenant table, so it has no parent-org column of its own.
    """

    __tablename__ = "organisations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    owner_user_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_organisations_slug_unique", "slug", unique=True),)


class OrgMember(Base):
    """Membership of a user in an organisation, with a role (owner|admin|member)."""

    __tablename__ = "org_members"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    organisation_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    org_role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    since: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    # one membership row per (org, user) — the unique edge
    __table_args__ = (
        Index("ix_org_members_org_user_unique", "organisation_id", "user_id", unique=True),
    )
