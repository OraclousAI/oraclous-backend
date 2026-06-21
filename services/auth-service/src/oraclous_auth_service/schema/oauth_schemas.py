"""OAuth request/response DTOs (schema layer)."""

from __future__ import annotations

from pydantic import BaseModel


class LoginUrlResponse(BaseModel):
    authorize_url: str


class ProvidersResponse(BaseModel):
    """The OAuth providers that have credentials configured (so the UI shows only working ones)."""

    providers: list[str]


class ConnectBeginRequest(BaseModel):
    """Begin a provider *connect* (G1): the FE callback URL the provider redirects to, plus the
    (tool) scopes to request. Empty scopes → the provider's default login scopes."""

    redirect_uri: str
    scopes: list[str] = []


class ConnectCompleteRequest(BaseModel):
    """Complete a provider connect: the code + state from the provider redirect. The user is taken
    from the authenticated bearer, never the body."""

    code: str
    state: str


class ConnectCompleteResponse(BaseModel):
    provider: str
    credential_id: str
