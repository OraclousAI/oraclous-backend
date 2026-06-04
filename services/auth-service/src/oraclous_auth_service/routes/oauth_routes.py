"""OAuth routes (ORAA-4 §21 routes layer).

`GET /oauth/{provider}/login` returns the provider authorize URL (PKCE challenge embedded).
`GET /oauth/{provider}/callback` exchanges the code and returns the app's tokens **in the body**
(never the URL — T-OAUTH leak). Thin handlers; OAuthService raises, factory maps to HTTP.
"""

from __future__ import annotations

from fastapi import APIRouter

from oraclous_auth_service.core.dependencies import OAuthServiceDep
from oraclous_auth_service.schema.auth_schemas import TokenResponse
from oraclous_auth_service.schema.oauth_schemas import LoginUrlResponse

router = APIRouter(prefix="/oauth", tags=["oauth"])


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
