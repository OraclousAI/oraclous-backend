"""OAuth provider registry (ORAA-4 §21 core layer — config).

Google / GitHub / Notion endpoint config + per-provider client id/secret read lazily from the env
(``OAUTH_<PROVIDER>_CLIENT_ID`` / ``_CLIENT_SECRET``). A provider with no configured client is
``None`` here → the OAuth routes return 503 for it, and the rest of the service still boots.

Redirect-URI allow-list (WP-11, threat T-OAUTH open-redirect / authorization-code theft): the
``redirect_uri`` a caller passes to ``/oauth/{provider}/login`` and ``/{provider}/connect`` is
client-supplied; left unchecked, an attacker can drive the PKCE handshake to a redirect they control
and capture the authorization code. Each provider carries a server-side allow-list resolved from
``OAUTH_<PROVIDER>_REDIRECT_URIS`` (comma-separated, exact-match).

Dev/local must keep working without ceremony, so the policy is run-mode-gated (see
``oauth_service._check_redirect_uri``):

* ``RUN_MODE != prod`` (dev/CI/local docker) with **no** env allow-list → permissive: any redirect
  is allowed (the local stack and the existing integration tests keep working). Set
  ``OAUTH_<PROVIDER>_REDIRECT_URIS`` in dev to opt into enforcement locally.
* ``RUN_MODE = prod`` → fail closed: an unset/empty allow-list denies **every** redirect, forcing
  the deploy to declare its real callback URLs explicitly. The example dev callbacks below are
  documentation of the expected shape, never an implicit prod default.

Example dev callbacks (NOT an implicit default — dev is permissive when the env var is unset):
``http://localhost:5173/oauth/callback``, ``http://localhost:3000/oauth/callback``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

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
    allowed_redirect_uris: tuple[str, ...] = field(default_factory=tuple)


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


def _allowed_redirect_uris(name: str) -> tuple[str, ...]:
    """Per-provider redirect-URI allow-list from ``OAUTH_<PROVIDER>_REDIRECT_URIS``
    (comma-separated, exact-match). An unset/empty env var yields an EMPTY tuple; the empty-list
    policy (permissive in dev, deny-all in prod) is applied by ``oauth_service._check_redirect_uri``
    so the run-mode gate lives in one place."""
    raw = os.environ.get(f"OAUTH_{name.upper()}_REDIRECT_URIS", "")
    return tuple(uri.strip() for uri in raw.split(",") if uri.strip())


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
        allowed_redirect_uris=_allowed_redirect_uris(name),
    )
