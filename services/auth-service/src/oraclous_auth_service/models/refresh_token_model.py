"""ORM model for refresh-token rotation families (R3.5-P3-S1, threat T-REFRESH).

Each issued refresh token has a row keyed by its JWT ``jti``. Refresh rotates: presenting a valid
refresh token marks its row ``rotated`` and issues a new token in the same ``family_id``. Presenting
an already-rotated/revoked token (reuse) is detected here and revokes the whole family — a stolen
refresh token cannot be replayed. ``organisation_id`` is carried per ADR-006.

No ``from __future__ import annotations`` — SQLAlchemy resolves ``Mapped[...]`` at mapper config.
"""

from datetime import datetime

from sqlalchemy import TIMESTAMP, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_auth_service.models.base import Base


class RefreshToken(Base):
    """A single issued refresh token within a rotation family."""

    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    organisation_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    jti: Mapped[str] = mapped_column(String, nullable=False)
    family_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    rotated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    # jti is the token's identity — unique so reuse detection is exact at the schema layer.
    __table_args__ = (Index("ix_refresh_tokens_jti_unique", "jti", unique=True),)
