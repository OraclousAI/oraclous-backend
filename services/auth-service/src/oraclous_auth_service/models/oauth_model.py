"""ORM models for OAuth: linked provider accounts + transient handshake state (R3.5-P3-S5).

``oauth_accounts`` is org-scoped (a user's linked provider tokens, encrypted at rest).
``oauth_states`` is pre-auth ephemeral handshake state (the PKCE verifier + redirect, single-use) —
it exists *before* any authentication, so it has no organisation scope.

No ``from __future__ import annotations`` — SQLAlchemy resolves ``Mapped[...]`` at mapper config.
"""

from datetime import datetime

from sqlalchemy import JSON, TIMESTAMP, Boolean, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_auth_service.models.base import Base


class OAuthAccount(Base):
    """A user's linked OAuth provider account. Access/refresh tokens are stored encrypted."""

    __tablename__ = "oauth_accounts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    organisation_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    access_token_enc: Mapped[str] = mapped_column(String, nullable=False)
    refresh_token_enc: Mapped[str | None] = mapped_column(String, nullable=True)
    scopes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_oauth_accounts_org_user_provider",
            "organisation_id",
            "user_id",
            "provider",
            unique=True,
        ),
    )


class OAuthState(Base):
    """Single-use OAuth handshake state holding the (encrypted) PKCE verifier + redirect.

    org-scoping: pre-auth-ephemeral — this row exists during the OAuth login handshake, before any
    principal/organisation is resolved, so it is deliberately not org-scoped. Consumed on callback
    (single-use → state-replay protection, T-OAUTH).
    """

    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    code_verifier_enc: Mapped[str] = mapped_column(String, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    consumed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
