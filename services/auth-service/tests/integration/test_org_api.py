"""Integration: organisations + membership + active-org selection + cross-org isolation (S2).

Real Postgres (testcontainers). Proves registration creates a real personal org (owner membership),
multi-org membership + `X-Organisation-Id` active-org selection embeds the right org in the token,
and a non-member cannot read/patch a foreign org (404 mask, T-ENUM/T-PRIV).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from jose import jwt

pytestmark = pytest.mark.integration

_SECRET = "integration-test-secret"  # noqa: S105 — matches the conftest fixture signing key


def _org_claim(access_token: str) -> str:
    return jwt.decode(access_token, _SECRET, algorithms=["HS256"])["organisation_id"]


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
    assert personal["slug"] == "alice-s-workspace"
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
    assert {o["slug"] for o in orgs} == {"bob-s-workspace", "side-project"}

    # login selecting the second org -> token carries that org
    login = await client.post(
        "/v1/auth/login",
        json={"email": "bob@ex.com", "password": "GoodPass1"},
        headers={"X-Organisation-Id": second["id"]},
    )
    assert login.status_code == 200
    assert _org_claim(login.json()["access_token"]) == second["id"]

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
