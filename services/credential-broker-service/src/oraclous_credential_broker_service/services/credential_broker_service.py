"""Runtime OAuth-token brokering (ORAA-4 §21 services layer, threats T-OAUTH-REFRESH / T6).

``get_provider_token`` resolves a fresh provider access token for (org, user, provider): decrypt the
stored OAuth credential, refresh it against the provider if near-expiry (re-encrypting the new grant
in place), and validate the caller's required scopes. Returns a success/error union — on scope
shortfall it returns the missing scopes + a re-consent ``login_url`` so the caller can drive
re-authorisation. The provider HTTP I/O is behind the ``RefreshClient`` port (key-free fake in CI).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from oraclous_credential_broker_service.core.config import get_settings
from oraclous_credential_broker_service.core.security import decrypt_secret
from oraclous_credential_broker_service.domain.errors import OAuthErrorCode
from oraclous_credential_broker_service.domain.scopes import missing_scopes
from oraclous_credential_broker_service.repositories.credential_repository import (
    CredentialRepository,
)
from oraclous_credential_broker_service.schema.credential_schema import RequestCredentials

_REFRESH_SKEW_SECONDS = 120


@dataclass(frozen=True, slots=True)
class TokenResult:
    success: bool
    access_token: str | None = None
    expires_at: str | None = None
    scopes: list[str] = field(default_factory=list)
    provider: str = ""
    error_code: str | None = None
    missing_scopes: list[str] | None = None
    login_url: str | None = None


class RefreshClient(Protocol):
    async def refresh(self, *, provider: str, refresh_token: str) -> dict:
        """Exchange a refresh token for a new grant: {access_token, refresh_token?, ...}."""
        ...


def _is_near_expiry(expires_at: object) -> bool:
    """True if the stored token is missing/expired/within the refresh skew window."""
    if not expires_at:
        return False  # no expiry recorded → treat as long-lived (don't force a refresh)
    try:
        exp = datetime.fromisoformat(str(expires_at))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
    except ValueError:
        return True  # unparseable → refresh to be safe
    return (exp - datetime.now(UTC)).total_seconds() <= _REFRESH_SKEW_SECONDS


class CredentialBrokerService:
    def __init__(self, *, credentials: CredentialRepository, refresh_client: RefreshClient) -> None:
        self._creds = credentials
        self._refresh = refresh_client

    def _login_url(self, provider: str) -> str:
        return f"{get_settings().AUTH_SERVICE_URL}/oauth/{provider}/login"

    async def get_provider_token(
        self,
        *,
        organisation_id: UUID,
        user_id: UUID,
        provider: str,
        required_scopes: list[str] | None = None,
    ) -> TokenResult:
        rows = await self._creds.list_credentials(
            RequestCredentials(user_id=user_id), organisation_id
        )
        row = next(
            (
                r
                for r in rows
                if r.provider == provider and str(getattr(r.cred_type, "value", "")) == "oauth"
            ),
            None,
        )
        if row is None:
            return TokenResult(
                success=False,
                provider=provider,
                error_code=OAuthErrorCode.TOKEN_NOT_FOUND.value,
                login_url=self._login_url(provider),
            )

        cred = decrypt_secret(row.encrypted_cred)
        granted = list(cred.get("scopes") or [])

        if _is_near_expiry(cred.get("expires_at")):
            refresh_token = cred.get("refresh_token")
            if not refresh_token:
                return TokenResult(
                    success=False,
                    provider=provider,
                    error_code=OAuthErrorCode.REFRESH_FAILED.value,
                    login_url=self._login_url(provider),
                )
            try:
                new_grant = await self._refresh.refresh(
                    provider=provider, refresh_token=refresh_token
                )
            except Exception:  # noqa: BLE001 — any refresh fault → structured REFRESH_FAILED
                return TokenResult(
                    success=False,
                    provider=provider,
                    error_code=OAuthErrorCode.REFRESH_FAILED.value,
                    login_url=self._login_url(provider),
                )
            cred = {**cred, **new_grant}
            granted = list(cred.get("scopes") or granted)
            await self._creds.update_encrypted_credential(row.id, organisation_id, cred)

        miss = missing_scopes(required_scopes, granted)
        if miss:
            return TokenResult(
                success=False,
                provider=provider,
                error_code=OAuthErrorCode.INSUFFICIENT_SCOPES.value,
                missing_scopes=miss,
                login_url=self._login_url(provider),
            )

        return TokenResult(
            success=True,
            access_token=cred.get("access_token"),
            expires_at=cred.get("expires_at"),
            scopes=granted,
            provider=provider,
        )
