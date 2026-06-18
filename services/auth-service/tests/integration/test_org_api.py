"""Integration: organisations + membership + active-org selection + cross-org isolation (S2).

Real Postgres (testcontainers). Proves registration creates a real personal org (owner membership),
multi-org membership + `X-Organisation-Id` active-org selection embeds the right org in the token,
and a non-member cannot read/patch a foreign org (404 mask, T-ENUM/T-PRIV).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from jose import jwt
from oraclous_governance import jwt_audience, jwt_issuer

pytestmark = pytest.mark.integration

_SECRET = "integration-test-secret"  # noqa: S105 — matches the conftest fixture signing key


def _org_claim(access_token: str) -> str:
    # iss/aud are stamped on every token (#356); pass them so the decode succeeds.
    return jwt.decode(
        access_token, _SECRET, algorithms=["HS256"], audience=jwt_audience(), issuer=jwt_issuer()
    )["organisation_id"]


async def _register(client: AsyncClient, email: str) -> dict:
    r = await client.post("/v1/auth/register", json={"email": email, "password": "GoodPass1"})
    assert r.status_code == 201, r.text
    return r.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_register_creates_a_real_personal_org(client: AsyncClient) -> None:
    body = await _register(client, "alice@ex.com")
    orgs = (await client.get("/v1/orgs", headers=_auth(body["access_token"]))).json()
    assert len(orgs) == 1
    personal = orgs[0]
    # no full_name at register -> the default org falls back to the email local-part (#317)
    assert personal["name"] == "alice's Second Mind"
    assert personal["slug"] == "alice-s-second-mind"
    # the token's organisation_id is the real personal org's id
    assert _org_claim(body["access_token"]) == personal["id"]


async def test_multi_org_active_selection_via_header(client: AsyncClient) -> None:
    body = await _register(client, "bob@ex.com")
    token = body["access_token"]
    # create a second org
    second = (
        await client.post("/v1/orgs", headers=_auth(token), json={"name": "Side Project"})
    ).json()
    orgs = (await client.get("/v1/orgs", headers=_auth(token))).json()
    assert {o["slug"] for o in orgs} == {"bob-s-second-mind", "side-project"}

    # login selecting the second org -> token carries that org
    login = await client.post(
        "/v1/auth/login",
        json={"email": "bob@ex.com", "password": "GoodPass1"},
        headers={"X-Organisation-Id": second["id"]},
    )
    assert login.status_code == 200
    assert _org_claim(login.json()["access_token"]) == second["id"]

    # /me follows the active org (the token claim), not the user's default personal org (#253)
    me = (await client.get("/v1/auth/me", headers=_auth(login.json()["access_token"]))).json()
    assert me["organisation_id"] == second["id"]

    # selecting an org the user does NOT belong to -> 404 (mask)
    bad = await client.post(
        "/v1/auth/login",
        json={"email": "bob@ex.com", "password": "GoodPass1"},
        headers={"X-Organisation-Id": "00000000-0000-0000-0000-0000deadbeef"},
    )
    assert bad.status_code == 404


async def test_cross_org_isolation(client: AsyncClient) -> None:
    a = await _register(client, "owner-a@ex.com")
    b = await _register(client, "owner-b@ex.com")
    a_org = (await client.get("/v1/orgs", headers=_auth(a["access_token"]))).json()[0]
    b_org = (await client.get("/v1/orgs", headers=_auth(b["access_token"]))).json()[0]

    # A can read + patch its own org
    assert (
        await client.get(f"/v1/orgs/{a_org['id']}", headers=_auth(a["access_token"]))
    ).status_code == 200
    patched = await client.patch(
        f"/v1/orgs/{a_org['id']}", headers=_auth(a["access_token"]), json={"name": "Renamed"}
    )
    assert patched.status_code == 200 and patched.json()["name"] == "Renamed"

    # A cannot see or patch B's org — masked as 404, never 403/leak
    assert (
        await client.get(f"/v1/orgs/{b_org['id']}", headers=_auth(a["access_token"]))
    ).status_code == 404
    assert (
        await client.patch(
            f"/v1/orgs/{b_org['id']}", headers=_auth(a["access_token"]), json={"name": "x"}
        )
    ).status_code == 404


async def test_org_routes_require_auth(client: AsyncClient) -> None:
    assert (await client.get("/v1/orgs")).status_code == 401
    assert (await client.post("/v1/orgs", json={"name": "x"})).status_code == 401


async def test_switch_org_reissues_token_and_survives_refresh(client: AsyncClient) -> None:
    body = await _register(client, "switch@ex.com")
    token = body["access_token"]
    personal_id = _org_claim(token)
    second = (await client.post("/v1/orgs", headers=_auth(token), json={"name": "Second"})).json()

    switched = await client.post(
        "/v1/auth/switch-org", headers={**_auth(token), "X-Organisation-Id": second["id"]}
    )
    assert switched.status_code == 200, switched.text
    new = switched.json()
    assert second["id"] != personal_id
    assert _org_claim(new["access_token"]) == second["id"]

    # the switched-to org is carried on the refresh token, so it survives a silent refresh
    refreshed = await client.post("/v1/auth/refresh", json={"refresh_token": new["refresh_token"]})
    assert refreshed.status_code == 200
    assert _org_claim(refreshed.json()["access_token"]) == second["id"]


async def test_switch_to_foreign_org_is_404(client: AsyncClient) -> None:
    a = await _register(client, "switch-a@ex.com")
    b = await _register(client, "switch-b@ex.com")
    b_org = (await client.get("/v1/orgs", headers=_auth(b["access_token"]))).json()[0]
    # A is not a member of B's org -> 404 mask (not 403)
    r = await client.post(
        "/v1/auth/switch-org",
        headers={**_auth(a["access_token"]), "X-Organisation-Id": b_org["id"]},
    )
    assert r.status_code == 404


async def test_switch_org_requires_auth_and_header(client: AsyncClient) -> None:
    # unauthenticated -> 401
    assert (
        await client.post("/v1/auth/switch-org", headers={"X-Organisation-Id": "x"})
    ).status_code == 401
    # the X-Organisation-Id header is required -> 422 when absent
    body = await _register(client, "switch-v@ex.com")
    assert (
        await client.post("/v1/auth/switch-org", headers=_auth(body["access_token"]))
    ).status_code == 422
