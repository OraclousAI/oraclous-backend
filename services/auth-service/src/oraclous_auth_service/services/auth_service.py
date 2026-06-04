"""User authentication use-cases (ORAA-4 §21 services layer).

All identity business logic lives here: registration, login, refresh-token rotation with reuse
detection, password change, and the `/me` projection. Routes parse + map HTTP only; repositories do
SQL only; this layer orchestrates them with the domain (password policy) and the core JWT issuer.

Threat posture: login + register return generic failures (no account enumeration, T-ENUM); a reused
refresh token revokes its whole family (T-REFRESH); every token carries the user's active
``organisation_id`` (ADR-006). S2: registration creates a real personal organisation (the user owns
it); the active org is a validated ``X-Organisation-Id`` selection at login (else the default),
and refresh preserves the active org from the presented refresh token.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jose import JWTError
from oraclous_auth_service.core.jwt_handler import (
    create_user_refresh_token,
    create_user_token,
    decode_token,
)
from oraclous_auth_service.domain.passwords import (
    hash_password,
    validate_password_strength,
    verify_password,
)
from oraclous_auth_service.models.user_model import User
from oraclous_auth_service.repositories.refresh_token_repository import RefreshTokenRepository
from oraclous_auth_service.repositories.user_repository import UserRepository
from oraclous_auth_service.services.org_service import OrgService


class AuthenticationError(Exception):
    """Credentials/token rejected — maps to HTTP 401."""


class EmailAlreadyRegisteredError(Exception):
    """Registration for an email that already exists — maps to HTTP 409."""


@dataclass(frozen=True, slots=True)
class TokenBundle:
    """An issued access+refresh pair (the service's return shape; routes map it to the DTO)."""

    user_id: str
    email: str
    organisation_id: str
    access_token: str
    refresh_token: str
    expires_in: int
    is_superuser: bool


class AuthService:
    def __init__(
        self,
        *,
        users: UserRepository,
        refresh_tokens: RefreshTokenRepository,
        orgs: OrgService,
    ) -> None:
        self._users = users
        self._refresh = refresh_tokens
        self._orgs = orgs

    async def _issue_pair(self, user: User, *, organisation_id: str, family_id: str) -> TokenBundle:
        access_token, expires_in = create_user_token(
            user_id=user.id, organisation_id=organisation_id, email=user.email
        )
        jti = str(uuid.uuid4())
        refresh_token, refresh_ttl = create_user_refresh_token(
            user_id=user.id,
            organisation_id=organisation_id,
            email=user.email,
            jti=jti,
        )
        await self._refresh.create(
            jti=jti,
            family_id=family_id,
            user_id=user.id,
            organisation_id=organisation_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=refresh_ttl),
        )
        return TokenBundle(
            user_id=user.id,
            email=user.email,
            organisation_id=organisation_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            is_superuser=user.is_superuser,
        )

    async def issue_for_user(self, *, user: User, organisation_id: str) -> TokenBundle:
        """Issue a fresh access+refresh pair for an already-authenticated user (e.g. via OAuth).

        Public entry point onto the same rotation-family issuance as register/login, for other
        services-layer flows that have authenticated a user out-of-band.
        """
        return await self._issue_pair(
            user, organisation_id=organisation_id, family_id=str(uuid.uuid4())
        )

    async def register(self, *, email: str, password: str) -> TokenBundle:
        validate_password_strength(password)
        if await self._users.get_by_email(email) is not None:
            raise EmailAlreadyRegisteredError("email already registered")
        # Create a real personal organisation (the user is its owner), then the user pointing at it.
        user_id = str(uuid.uuid4())
        local = email.split("@", 1)[0]
        org = await self._orgs.create_org(name=f"{local}'s workspace", owner_user_id=user_id)
        user = await self._users.create_user(
            id=user_id,
            email=email,
            password_hash=hash_password(password),
            default_organisation_id=org.id,
        )
        return await self._issue_pair(user, organisation_id=org.id, family_id=str(uuid.uuid4()))

    async def login(
        self, *, email: str, password: str, requested_org_id: str | None = None
    ) -> TokenBundle:
        user = await self._users.get_by_email(email)
        # Generic failure on both unknown-email and bad-password (no enumeration, T-ENUM).
        if user is None or not verify_password(password, user.password_hash):
            raise AuthenticationError("invalid email or password")
        if not user.is_active:
            raise AuthenticationError("account is disabled")
        organisation_id = await self._orgs.resolve_active_org(
            user=user, requested_org_id=requested_org_id
        )
        return await self._issue_pair(
            user, organisation_id=organisation_id, family_id=str(uuid.uuid4())
        )

    async def refresh(self, *, refresh_token: str) -> TokenBundle:
        try:
            claims = decode_token(refresh_token)
        except JWTError as exc:
            raise AuthenticationError("invalid refresh token") from exc
        if claims.get("type") != "refresh":
            raise AuthenticationError("not a refresh token")
        jti = claims.get("jti")
        row = await self._refresh.get_by_jti(jti) if jti else None
        if row is None:
            raise AuthenticationError("unknown refresh token")
        if row.status != "active":
            # Reuse of a rotated/revoked token: a stolen token is being replayed — kill the family.
            await self._refresh.revoke_family(row.family_id)
            raise AuthenticationError("refresh token reuse detected")
        user = await self._users.get_by_id(row.user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("user no longer active")
        await self._refresh.mark_rotated(jti, rotated_at=datetime.now(UTC))
        # Preserve the active org the refresh token was issued for.
        organisation_id = claims.get("organisation_id") or user.default_organisation_id
        return await self._issue_pair(
            user, organisation_id=organisation_id, family_id=row.family_id
        )

    async def change_password(self, *, user_id: str, new_password: str) -> None:
        validate_password_strength(new_password)
        user = await self._users.set_password(user_id, hash_password(new_password))
        if user is None:
            raise AuthenticationError("user not found")

    async def get_user(self, *, user_id: str) -> User:
        user = await self._users.get_by_id(user_id)
        if user is None or not user.is_active:
            # Revocation race (T2): a disabled/deleted user can't re-authenticate even with a live
            # unexpired token.
            raise AuthenticationError("user not found or disabled")
        return user
