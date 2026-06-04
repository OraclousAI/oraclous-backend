"""ORM model for the human user principal (R3.5-P3-S1).

Reshaped from the legacy ``auth-service/app/models/user_model.User``. Differences:

* ``password_hash`` is nullable (an OAuth-only user has no password — S5).
* ``default_organisation_id`` is the user's active organisation: in S1 it is a generated
  personal-org id (the token's ``organisation_id`` claim) so identity authorises end-to-end before
  multi-org membership lands in S2, where it becomes a real ``organisations`` row + ``org_members``
  edge and the active-org selection generalises to many orgs.
* Email verification + password reset use short-lived signed *purpose* JWTs, not a 6-digit DB
  column, eliminating the legacy verification-code brute-force surface (T-VERIFY).

No ``from __future__ import annotations`` — SQLAlchemy resolves ``Mapped[...]`` at mapper config.
"""

from datetime import datetime

from sqlalchemy import TIMESTAMP, Boolean, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_auth_service.models.base import Base


class User(Base):
    """A human user. Email is the unique login handle (stored lowercased).

    org-scoping: cross-org-principal — a human belongs to many organisations via membership (S2), so
    this identity table is deliberately not org-scoped; the active org is carried on the issued token
    (``default_organisation_id`` until S2) and the ``org_members`` edge, never as a column here.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    default_organisation_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_name: Mapped[str | None] = mapped_column(String, nullable=True)
    last_name: Mapped[str | None] = mapped_column(String, nullable=True)
    profile_picture: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # One identity per email (lowercased by the repository before write/read). Unique at the schema
    # layer so a race cannot create two users for one email regardless of app wiring (T-ENUM/T-DUP).
    __table_args__ = (Index("ix_users_email_unique", "email", unique=True),)
