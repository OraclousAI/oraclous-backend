"""OAuth routes (ORAA-4 §21 routes layer).

`GET /oauth/{provider}/login` returns the provider authorize URL (PKCE challenge embedded).
`GET /oauth/{provider}/callback` exchanges the code and returns the app's tokens **in the body**
(never the URL — T-OAUTH leak). Thin handlers; OAuthService raises, factory maps to HTTP.
"""

from __future__ import annotations

from fastapi import APIRouter

from oraclous_auth_service.core.dependencies import OAuthServiceDep, UserClaimsDep
from oraclous_auth_service.schema.auth_schemas import TokenResponse
from oraclous_auth_service.schema.oauth_schemas import (
    ConnectBeginRequest,
    ConnectCompleteRequest,
    ConnectCompleteResponse,
    LoginUrlResponse,
    ProvidersResponse,
)

router = APIRouter(prefix="/oauth", tags=["oauth"])


# Declared before the parameterized /{provider}/* routes so the literal path matches first.
@router.get("/providers", response_model=ProvidersResponse)
async def oauth_providers(oauth: OAuthServiceDep) -> ProvidersResponse:
    return ProvidersResponse(providers=oauth.available_providers())


@router.get("/{provider}/login", response_model=LoginUrlResponse)
async def oauth_login(provider: str, redirect_uri: str, oauth: OAuthServiceDep) -> LoginUrlResponse:
    url = await oauth.begin_login(provider_name=provider, redirect_uri=redirect_uri)
    return LoginUrlResponse(authorize_url=url)


@router.get("/{provider}/callback", response_model=TokenResponse)
async def oauth_callback(
    provider: str, code: str, state: str, oauth: OAuthServiceDep
) -> TokenResponse:
    bundle = await oauth.complete_callback(provider_name=provider, code=code, state=state)
    return TokenResponse(
        access_token=bundle.access_token,
        refresh_token=bundle.refresh_token,
        expires_in=bundle.expires_in,
        email=bundle.email,
        is_superuser=bundle.is_superuser,
    )


# --- provider connect (G1): authenticated — binds a provider token to the caller, no session ---
@router.post("/{provider}/connect", response_model=LoginUrlResponse)
async def oauth_connect_begin(
    provider: str, body: ConnectBeginRequest, claims: UserClaimsDep, oauth: OAuthServiceDep
) -> LoginUrlResponse:
    """Begin a provider *connect* for the authenticated caller: return the authorize URL requesting
    the given (tool) scopes. The caller is bound at ``/connect/complete``, never here."""
    url = await oauth.begin_connect(
        provider_name=provider, redirect_uri=body.redirect_uri, scopes=body.scopes
    )
    return LoginUrlResponse(authorize_url=url)


@router.post("/{provider}/connect/complete", response_model=ConnectCompleteResponse)
async def oauth_connect_complete(
    provider: str, body: ConnectCompleteRequest, claims: UserClaimsDep, oauth: OAuthServiceDep
) -> ConnectCompleteResponse:
    """Complete a provider connect: exchange the code and land the token as a resolvable broker
    credential for the authenticated caller. org/user come from the bearer, never the body."""
    credential_id = await oauth.complete_connect(
        provider_name=provider,
        code=body.code,
        state=body.state,
        organisation_id=claims["organisation_id"],
        user_id=claims["sub"],
    )
    return ConnectCompleteResponse(provider=provider, credential_id=credential_id)
