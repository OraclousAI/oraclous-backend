"""Integration: invitation lifecycle vs real Postgres (S3).

invite (admin-gated) → peek → accept → real membership; supersede prior pending; revoke; generic-400
for any bad/replayed token; admin-gate on create. Uses the shared `client` fixture (conftest).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(client: AsyncClient, email: str) -> str:
    r = await client.post("/v1/auth/register", json={"email": email, "password": "GoodPass1"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


async def _personal_org(client: AsyncClient, token: str) -> str:
    return (await client.get("/v1/orgs", headers=_auth(token))).json()[0]["id"]


async def test_invite_accept_creates_membership(client: AsyncClient) -> None:
    owner = await _register(client, "owner@ex.com")
    org_id = await _personal_org(client, owner)

    inv = await client.post(
        f"/v1/orgs/{org_id}/invitations", headers=_auth(owner), json={"email": "bob@ex.com"}
    )
    assert inv.status_code == 201, inv.text
    token = inv.json()["token"]

    # public peek shows the org + role without auth
    peek = await client.post("/v1/invitations/peek", json={"token": token})
    assert peek.status_code == 200
    assert peek.json()["organisation_id"] == org_id and peek.json()["role"] == "member"

    # bob registers and accepts -> becomes a member of owner's org
    bob = await _register(client, "bob@ex.com")
    acc = await client.post("/v1/invitations/accept", headers=_auth(bob), json={"token": token})
    assert acc.status_code == 200 and acc.json()["organisation_id"] == org_id

    bob_orgs = {o["id"] for o in (await client.get("/v1/orgs", headers=_auth(bob))).json()}
    assert org_id in bob_orgs  # the invited org is now visible to bob
    assert (await client.get(f"/v1/orgs/{org_id}", headers=_auth(bob))).status_code == 200

    # the token is single-use: replay -> generic 400
    replay = await client.post("/v1/invitations/accept", headers=_auth(bob), json={"token": token})
    assert replay.status_code == 400


async def test_supersede_invalidates_prior_pending(client: AsyncClient) -> None:
    owner = await _register(client, "owner2@ex.com")
    org_id = await _personal_org(client, owner)
    first = (
        await client.post(
            f"/v1/orgs/{org_id}/invitations", headers=_auth(owner), json={"email": "x@ex.com"}
        )
    ).json()["token"]
    second = (
        await client.post(
            f"/v1/orgs/{org_id}/invitations", headers=_auth(owner), json={"email": "x@ex.com"}
        )
    ).json()["token"]
    # the first (superseded) token is now invalid; the second works
    assert (await client.post("/v1/invitations/peek", json={"token": first})).status_code == 400
    assert (await client.post("/v1/invitations/peek", json={"token": second})).status_code == 200


async def test_revoke_then_accept_is_generic_400(client: AsyncClient) -> None:
    owner = await _register(client, "owner3@ex.com")
    org_id = await _personal_org(client, owner)
    created = (
        await client.post(
            f"/v1/orgs/{org_id}/invitations", headers=_auth(owner), json={"email": "y@ex.com"}
        )
    ).json()
    assert (
        await client.delete(f"/v1/orgs/{org_id}/invitations/{created['id']}", headers=_auth(owner))
    ).status_code == 204
    bob = await _register(client, "bob3@ex.com")
    assert (
        await client.post(
            "/v1/invitations/accept", headers=_auth(bob), json={"token": created["token"]}
        )
    ).status_code == 400


async def test_only_admins_can_invite(client: AsyncClient) -> None:
    owner = await _register(client, "owner4@ex.com")
    org_id = await _personal_org(client, owner)
    member_inv = (
        await client.post(
            f"/v1/orgs/{org_id}/invitations", headers=_auth(owner), json={"email": "m@ex.com"}
        )
    ).json()["token"]
    member = await _register(client, "member4@ex.com")
    await client.post("/v1/invitations/accept", headers=_auth(member), json={"token": member_inv})
    # a plain member cannot create invitations -> 403
    assert (
        await client.post(
            f"/v1/orgs/{org_id}/invitations", headers=_auth(member), json={"email": "z@ex.com"}
        )
    ).status_code == 403
    # a non-member cannot even see the org -> 404 (mask)
    outsider = await _register(client, "outsider4@ex.com")
    assert (
        await client.post(
            f"/v1/orgs/{org_id}/invitations", headers=_auth(outsider), json={"email": "z@ex.com"}
        )
    ).status_code == 404


async def test_bad_token_and_auth(client: AsyncClient) -> None:
    assert (
        await client.post("/v1/invitations/peek", json={"token": "nope-not-a-real-token"})
    ).status_code == 400
    # accept requires auth
    assert (await client.post("/v1/invitations/accept", json={"token": "x"})).status_code == 401
