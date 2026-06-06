"""OAuth login use-cases (ORAA-4 §21 services layer, threats T-OAUTH / T-OAUTH-PLAINTEXT).

begin_login → a signed-free PKCE handshake: generate state + verifier (verifier kept server-side,
encrypted, in oauth_states), build the provider authorize URL with the S256 challenge. callback →
consume the single-use state, exchange the code with the verifier, fetch the profile, upsert the
user (+ a personal org for a first-time login), store the provider tokens **encrypted at rest**
(scope set-union), and issue the app's own user JWT — returned in the response body, never the URL.

The provider HTTP I/O is behind the ``ProviderClient`` port so the flow is testable with a fake
provider (key-free CI); the real httpx client lives in ``oauth_provider_client.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode

from oraclous_auth_service.core.encryption import decrypt, encrypt
from oraclous_auth_service.core.oauth_providers import SUPPORTED, ProviderConfig, get_provider
from oraclous_auth_service.domain.oauth import generate_pkce, generate_state, merge_scopes
from oraclous_auth_service.repositories.audit_repository import AuditRepository
from oraclous_auth_service.repositories.oauth_repository import (
    OAuthAccountRepository,
    OAuthStateRepository,
)
from oraclous_auth_service.repositories.user_repository import UserRepository, normalize_email
from oraclous_auth_service.services.auth_service import AuthService, TokenBundle
from oraclous_auth_service.services.org_service import OrgService

_STATE_TTL_SECONDS = 600


@dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    scopes: list[str]
    expires_in: int | None


@dataclass(frozen=True, slots=True)
class ProfileInfo:
    external_id: str
    email: str
    name: str | None = None


class ProviderClient(Protocol):
    async def exchange_code(
        self, provider: ProviderConfig, *, code: str, code_verifier: str, redirect_uri: str
    ) -> TokenSet: ...

    async def fetch_userinfo(self, provider: ProviderConfig, token: TokenSet) -> ProfileInfo: ...


class OAuthError(Exception):
    """Invalid state / code / profile — maps to a generic HTTP 400 (no oracle)."""


class OAuthProviderUnconfiguredError(Exception):
    """The provider has no client credentials configured — maps to HTTP 503."""


class OAuthService:
    def __init__(
        self,
        *,
        users: UserRepository,
        orgs: OrgService,
        auth: AuthService,
        accounts: OAuthAccountRepository,
        states: OAuthStateRepository,
        client: ProviderClient,
        audit: AuditRepository | None = None,
    ) -> None:
        self._users = users
        self._orgs = orgs
        self._auth = auth
        self._accounts = accounts
        self._states = states
        self._client = client
        self._audit = audit

    @staticmethod
    def _provider_or_503(name: str) -> ProviderConfig:
        provider = get_provider(name)
        if provider is None:
            raise OAuthProviderUnconfiguredError(f"OAuth provider '{name}' is not configured")
        return provider

    @staticmethod
    def available_providers() -> list[str]:
        """Supported providers that have credentials configured (names only — no secrets)."""
        return [name for name in SUPPORTED if get_provider(name) is not None]

    async def begin_login(self, *, provider_name: str, redirect_uri: str) -> str:
        provider = self._provider_or_503(provider_name)
        state = generate_state()
        verifier, challenge = generate_pkce()
        await self._states.create(
            state=state,
            provider=provider_name,
            code_verifier_enc=encrypt(verifier),
            redirect_uri=redirect_uri,
            expires_at=datetime.now(UTC) + timedelta(seconds=_STATE_TTL_SECONDS),
        )
        params = {
            "response_type": "code",
            "client_id": provider.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(provider.default_scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{provider.authorize_url}?{urlencode(params)}"

    async def complete_callback(self, *, provider_name: str, code: str, state: str) -> TokenBundle:
        provider = self._provider_or_503(provider_name)
        row = await self._states.consume(state=state, now=datetime.now(UTC))
        if row is None or row.provider != provider_name:
            raise OAuthError("invalid or expired oauth state")
        verifier = decrypt(row.code_verifier_enc)
        token_set = await self._client.exchange_code(
            provider, code=code, code_verifier=verifier, redirect_uri=row.redirect_uri
        )
        profile = await self._client.fetch_userinfo(provider, token_set)
        if not profile.email:
            raise OAuthError("oauth provider did not return an email")

        user = await self._upsert_user(profile)
        await self._store_tokens(user, provider_name, token_set)
        if self._audit is not None:
            await self._audit.record(
                event="oauth.login",
                actor_type="user",
                actor_id=user.id,
                organisation_id=user.default_organisation_id,
                target=provider_name,
            )
        return await self._auth.issue_for_user(
            user=user, organisation_id=user.default_organisation_id
        )

    async def _upsert_user(self, profile: ProfileInfo):
        user = await self._users.get_by_email(profile.email)
        if user is not None:
            return user
        import uuid

        user_id = str(uuid.uuid4())
        local = normalize_email(profile.email).split("@", 1)[0]
        org = await self._orgs.create_org(name=f"{local}'s workspace", owner_user_id=user_id)
        return await self._users.create_user(
            id=user_id,
            email=profile.email,
            password_hash=None,  # OAuth-only user has no password
            default_organisation_id=org.id,
            first_name=profile.name,
            is_email_verified=True,  # the provider verified the email
        )

    async def _store_tokens(self, user, provider_name: str, token_set: TokenSet) -> None:
        existing = await self._accounts.get(
            organisation_id=user.default_organisation_id, user_id=user.id, provider=provider_name
        )
        merged = merge_scopes(existing.scopes if existing else None, token_set.scopes)
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=token_set.expires_in)
            if token_set.expires_in
            else None
        )
        await self._accounts.upsert(
            organisation_id=user.default_organisation_id,
            user_id=user.id,
            provider=provider_name,
            access_token_enc=encrypt(token_set.access_token),
            refresh_token_enc=encrypt(token_set.refresh_token) if token_set.refresh_token else None,
            scopes=merged,
            expires_at=expires_at,
        )
