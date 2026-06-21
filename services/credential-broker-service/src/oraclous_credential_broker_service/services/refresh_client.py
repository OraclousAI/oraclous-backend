"""Real provider refresh client (services layer — external integration).

Implements the ``RefreshClient`` port against the live Google / GitHub / Notion token endpoints via
httpx, using the broker's own provider client credentials (env). Automated CI uses a fake client
(key-free); this real client is exercised by the human real-key sign-off (needs-human).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx

_TIMEOUT = httpx.Timeout(10.0)

_TOKEN_URLS = {
    "google": "https://oauth2.googleapis.com/token",
    "github": "https://github.com/login/oauth/access_token",
    "notion": "https://api.notion.com/v1/oauth/token",
}


class RefreshError(RuntimeError):
    """Provider refresh failed (unknown provider, missing creds, non-200, or no access_token)."""


class HttpxRefreshClient:
    async def refresh(self, *, provider: str, refresh_token: str) -> dict:
        url = _TOKEN_URLS.get(provider)
        if url is None:
            raise RefreshError(f"unknown provider '{provider}'")
        client_id = os.environ.get(f"OAUTH_{provider.upper()}_CLIENT_ID", "")
        client_secret = os.environ.get(f"OAUTH_{provider.upper()}_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise RefreshError(f"no client credentials configured for '{provider}'")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            resp = await http.post(url, data=data, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            raise RefreshError("provider refresh returned non-200")
        body = resp.json()
        if "access_token" not in body:
            raise RefreshError("provider refresh returned no access_token")
        out: dict = {"access_token": body["access_token"]}
        if body.get("refresh_token"):
            out["refresh_token"] = body["refresh_token"]
        if body.get("scope"):
            out["scopes"] = str(body["scope"]).replace(",", " ").split()
        if body.get("expires_in"):
            out["expires_at"] = (
                datetime.now(UTC) + timedelta(seconds=int(body["expires_in"]))
            ).isoformat()
        return out
