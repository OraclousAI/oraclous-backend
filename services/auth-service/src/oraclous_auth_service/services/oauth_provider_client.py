"""Real OAuth provider HTTP client (services layer — external integration).

Implements the ``ProviderClient`` port against the live Google / GitHub / Notion endpoints over
httpx.
The PKCE ``code_verifier`` is sent at token exchange; provider responses are normalised to the
generic ``TokenSet`` / ``ProfileInfo``. CI verifies the flow with a fake client (key-free);
this real client is exercised by the human real-key sign-off (needs-human).
"""

from __future__ import annotations

import base64

import httpx
from oraclous_auth_service.core.oauth_providers import ProviderConfig
from oraclous_auth_service.services.oauth_service import OAuthError, ProfileInfo, TokenSet

_TIMEOUT = httpx.Timeout(10.0)


def _split_scopes(raw: object) -> list[str]:
    if isinstance(raw, str):
        return [s for s in raw.replace(",", " ").split() if s]
    if isinstance(raw, list):
        return [str(s) for s in raw]
    return []


class HttpxProviderClient:
    async def exchange_code(
        self, provider: ProviderConfig, *, code: str, code_verifier: str, redirect_uri: str
    ) -> TokenSet:
        headers = {"Accept": "application/json"}
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
        if provider.name == "notion":
            # Notion authenticates the client with HTTP Basic, not body params.
            basic = base64.b64encode(
                f"{provider.client_id}:{provider.client_secret}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {basic}"
        else:
            data["client_id"] = provider.client_id
            data["client_secret"] = provider.client_secret
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            resp = await http.post(provider.token_url, data=data, headers=headers)
        if resp.status_code != 200:
            raise OAuthError("oauth token exchange failed")
        body = resp.json()
        if "access_token" not in body:
            raise OAuthError("oauth token exchange returned no access_token")
        return TokenSet(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            scopes=_split_scopes(body.get("scope")),
            expires_in=body.get("expires_in"),
        )

    async def fetch_userinfo(self, provider: ProviderConfig, token: TokenSet) -> ProfileInfo:
        if provider.name == "notion":
            # Notion has no userinfo endpoint; the bot token's /users/me identifies the owner.
            return await self._notion_userinfo(token)
        headers = {
            "Authorization": f"Bearer {token.access_token}",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            resp = await http.get(provider.userinfo_url, headers=headers)
            if resp.status_code != 200:
                raise OAuthError("oauth userinfo fetch failed")
            info = resp.json()
            email = info.get("email")
            if not email and provider.name == "github":
                email = await self._github_primary_email(http, headers)
        external_id = str(info.get("sub") or info.get("id") or "")
        name = info.get("name") or info.get("login")
        if not email:
            raise OAuthError("oauth provider did not expose a verified email")
        return ProfileInfo(external_id=external_id, email=email, name=name)

    async def _github_primary_email(self, http: httpx.AsyncClient, headers: dict) -> str | None:
        resp = await http.get("https://api.github.com/user/emails", headers=headers)
        if resp.status_code != 200:
            return None
        for entry in resp.json():
            if entry.get("primary") and entry.get("verified"):
                return entry.get("email")
        return None

    async def _notion_userinfo(self, token: TokenSet) -> ProfileInfo:
        headers = {
            "Authorization": f"Bearer {token.access_token}",
            "Notion-Version": "2022-06-28",
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            resp = await http.get("https://api.notion.com/v1/users/me", headers=headers)
        if resp.status_code != 200:
            raise OAuthError("notion userinfo fetch failed")
        bot = resp.json()
        owner = (bot.get("bot") or {}).get("owner", {}).get("user", {})
        person = owner.get("person", {})
        email = person.get("email")
        if not email:
            raise OAuthError("notion did not expose an owner email")
        return ProfileInfo(
            external_id=str(owner.get("id") or ""), email=email, name=owner.get("name")
        )
