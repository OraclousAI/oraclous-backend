"""OAuth provider registry (ORAA-4 §21 core layer — config).

Google / GitHub / Notion endpoint config + per-provider client id/secret read lazily from the env
(``OAUTH_<PROVIDER>_CLIENT_ID`` / ``_CLIENT_SECRET``). A provider with no configured client is
``None`` here → the OAuth routes return 503 for it, and the rest of the service still boots.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

SUPPORTED = ("google", "github", "notion")


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    default_scopes: tuple[str, ...]


_ENDPOINTS: dict[str, dict] = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo",
        "default_scopes": ("openid", "email", "profile"),
    },
    "github": {
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "userinfo_url": "https://api.github.com/user",
        "default_scopes": ("read:user", "user:email"),
    },
    "notion": {
        "authorize_url": "https://api.notion.com/v1/oauth/authorize",
        "token_url": "https://api.notion.com/v1/oauth/token",
        "userinfo_url": "",  # Notion returns the owner in the token response — no separate call
        "default_scopes": (),
    },
}


def get_provider(name: str) -> ProviderConfig | None:
    """Return the configured provider, or ``None`` if unknown / no client credentials in env."""
    spec = _ENDPOINTS.get(name)
    if spec is None:
        return None
    client_id = os.environ.get(f"OAUTH_{name.upper()}_CLIENT_ID", "")
    client_secret = os.environ.get(f"OAUTH_{name.upper()}_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None
    return ProviderConfig(
        name=name,
        client_id=client_id,
        client_secret=client_secret,
        authorize_url=spec["authorize_url"],
        token_url=spec["token_url"],
        userinfo_url=spec["userinfo_url"],
        default_scopes=spec["default_scopes"],
    )
