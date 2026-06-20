"""OAuth login journey END-TO-END through the API GATEWAY against a REAL OIDC provider (dex).

A real user logs in via OAuth: the gateway begins the login (authorize URL), the user authenticates
at a **real dex** with a **real password** (dex's local username/password connector — not a mock
provider), dex redirects back with an authorization code, the gateway completes the callback and
issues an Oraclous session JWT, and that JWT is a real session (`/me` returns the dex user). The
full authorization-code + PKCE dance runs against the real IdP; nothing is mocked or stubbed.

Requires the dex provider (`scripts/e2e.sh --oauth` brings up dex + configures the auth-service).
Skipped when dex (`:5556`) is unreachable, so it never reddens the rest of the suite or unit CI.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.oauth, pytest.mark.security]

GATEWAY = "http://localhost:8006"
DEX = "http://localhost:5556"
REDIRECT = "http://localhost:9999/cb"  # a placeholder the test captures (no real server needed)
DEX_USER, DEX_PASSWORD = "oauthuser@oraclous.test", "Password123!"


def _dex_up() -> bool:
    try:
        return (
            httpx.get(f"{DEX}/dex/.well-known/openid-configuration", timeout=2.0).status_code == 200
        )
    except httpx.HTTPError:
        return False


requires_dex = pytest.mark.skipif(
    not _dex_up(), reason="dex :5556 not reachable — run scripts/e2e.sh --oauth"
)


def _follow_until_callback(c: httpx.Client, method: str, url: str, **kw: object):
    """Follow redirects manually until the IdP bounces back to the (server-less) REDIRECT, which we
    capture instead of fetching; otherwise return the final page (e.g. the login form)."""
    for _ in range(12):
        r = c.request(method, url, follow_redirects=False, **kw)  # type: ignore[arg-type]
        method, kw = "GET", {}
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers["location"]
            if not loc.startswith("http"):
                loc = DEX + loc
            if "localhost:9999" in loc:
                return loc, r
            url = loc
        else:
            return None, r
    return None, r


@requires_dex
def test_a_user_logs_in_via_oauth_through_the_gateway() -> None:
    # 1. the gateway begins the login → the real dex authorize URL (with PKCE + state)
    authorize = httpx.get(
        f"{GATEWAY}/oauth/dex/login", params={"redirect_uri": REDIRECT}, timeout=15.0
    ).json()["authorize_url"]
    assert authorize.startswith(f"{DEX}/dex/auth")

    # 2. the user authenticates at the REAL dex with a REAL password (the local connector)
    c = httpx.Client(timeout=15.0)
    _, page = _follow_until_callback(c, "GET", authorize)
    action = html.unescape(re.search(r'action="([^"]+)"', page.text).group(1))
    login_url = action if action.startswith("http") else DEX + action
    callback, _ = _follow_until_callback(
        c, "POST", login_url, data={"login": DEX_USER, "password": DEX_PASSWORD}
    )
    assert callback, "dex did not redirect back with an authorization code"
    q = parse_qs(urlparse(callback).query)

    # 3. the gateway completes the callback → an Oraclous session JWT
    tok = httpx.get(
        f"{GATEWAY}/oauth/dex/callback",
        params={"code": q["code"][0], "state": q["state"][0]},
        timeout=15.0,
    )
    assert tok.status_code == 200, tok.text
    jwt = tok.json()["access_token"]

    # 4. the OAuth-issued JWT is a real session — /me returns the dex user, through the gateway
    me = httpx.get(
        f"{GATEWAY}/v1/auth/me", headers={"Authorization": f"Bearer {jwt}"}, timeout=15.0
    )
    assert me.status_code == 200, me.text
    assert me.json()["email"] == DEX_USER


@requires_dex
def test_oauth_callback_rejects_a_bogus_state_through_the_gateway() -> None:
    # a callback whose state was never issued is rejected (single-use server-side state, no oracle)
    bogus = httpx.get(
        f"{GATEWAY}/oauth/dex/callback",
        params={"code": "whatever", "state": "never-issued-state"},
        timeout=15.0,
    )
    assert bogus.status_code in (400, 401, 403)
