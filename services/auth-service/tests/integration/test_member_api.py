"""Integration: org member roster + role management vs real Postgres.

List the roster (with emails); promote/demote and remove members; admin-gated with the
actor-must-strictly-outrank-target rule (the owner is immutable, peers and self are protected);
non-members are 404-masked. Uses the shared `client` fixture (conftest).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.integration, pytest.mark.api_authz]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(client: AsyncClient, email: str) -> str:
    r = await client.post("/v1/auth/register", json={"email": email, "password": "GoodPass1"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


async def _personal_org(client: AsyncClient, token: str) -> str:
    return (await client.get("/v1/orgs", headers=_auth(token))).json()[0]["id"]


async def _me_id(client: AsyncClient, token: str) -> str:
    return (await client.get("/v1/auth/me", headers=_auth(token))).json()["id"]


async def _add_member(
    client: AsyncClient, *, owner_token: str, org_id: str, email: str, member_token: str
) -> str:
    """Invite ``email`` into ``org_id`` and accept as ``member_token``; returns the member's id."""
    inv = await client.post(
        f"/v1/orgs/{org_id}/invitations", headers=_auth(owner_token), json={"email": email}
    )
    assert inv.status_code == 201, inv.text
    acc = await client.post(
        "/v1/invitations/accept", headers=_auth(member_token), json={"token": inv.json()["token"]}
    )
    assert acc.status_code == 200, acc.text
    return await _me_id(client, member_token)


async def test_owner_sees_roster_with_email(client: AsyncClient) -> None:
    owner = await _register(client, "owner@m.com")
    org_id = await _personal_org(client, owner)
    r = await client.get(f"/v1/orgs/{org_id}/members", headers=_auth(owner))
    assert r.status_code == 200, r.text
    roster = r.json()
    assert len(roster) == 1
    assert roster[0]["role"] == "owner"
    assert roster[0]["email"] == "owner@m.com"
    assert "since" in roster[0]


async def test_promote_then_remove_member(client: AsyncClient) -> None:
    owner = await _register(client, "owner2@m.com")
    org_id = await _personal_org(client, owner)
    bob = await _register(client, "bob2@m.com")
    bob_id = await _add_member(
        client, owner_token=owner, org_id=org_id, email="bob2@m.com", member_token=bob
    )

    roster = (await client.get(f"/v1/orgs/{org_id}/members", headers=_auth(owner))).json()
    assert {m["role"] for m in roster} == {"owner", "member"}

    promoted = await client.patch(
        f"/v1/orgs/{org_id}/members/{bob_id}", headers=_auth(owner), json={"role": "admin"}
    )
    assert promoted.status_code == 200 and promoted.json()["role"] == "admin"

    removed = await client.delete(f"/v1/orgs/{org_id}/members/{bob_id}", headers=_auth(owner))
    assert removed.status_code == 204
    roster = (await client.get(f"/v1/orgs/{org_id}/members", headers=_auth(owner))).json()
    assert [m["role"] for m in roster] == ["owner"]


async def test_owner_is_immutable(client: AsyncClient) -> None:
    owner = await _register(client, "owner3@m.com")
    org_id = await _personal_org(client, owner)
    owner_id = await _me_id(client, owner)
    admin = await _register(client, "admin3@m.com")
    admin_id = await _add_member(
        client, owner_token=owner, org_id=org_id, email="admin3@m.com", member_token=admin
    )
    await client.patch(
        f"/v1/orgs/{org_id}/members/{admin_id}", headers=_auth(owner), json={"role": "admin"}
    )
    # an admin cannot demote or remove the owner (outranks rule) -> 403
    assert (
        await client.patch(
            f"/v1/orgs/{org_id}/members/{owner_id}", headers=_auth(admin), json={"role": "member"}
        )
    ).status_code == 403
    assert (
        await client.delete(f"/v1/orgs/{org_id}/members/{owner_id}", headers=_auth(admin))
    ).status_code == 403
    # the owner cannot remove themselves either (equal rank / self) -> 403
    assert (
        await client.delete(f"/v1/orgs/{org_id}/members/{owner_id}", headers=_auth(owner))
    ).status_code == 403


async def test_admin_cannot_manage_a_peer_admin(client: AsyncClient) -> None:
    owner = await _register(client, "owner4@m.com")
    org_id = await _personal_org(client, owner)
    a = await _register(client, "a4@m.com")
    a_id = await _add_member(
        client, owner_token=owner, org_id=org_id, email="a4@m.com", member_token=a
    )
    b = await _register(client, "b4@m.com")
    b_id = await _add_member(
        client, owner_token=owner, org_id=org_id, email="b4@m.com", member_token=b
    )
    for uid in (a_id, b_id):
        await client.patch(
            f"/v1/orgs/{org_id}/members/{uid}", headers=_auth(owner), json={"role": "admin"}
        )
    # admin A cannot change or remove peer admin B -> 403
    assert (
        await client.patch(
            f"/v1/orgs/{org_id}/members/{b_id}", headers=_auth(a), json={"role": "member"}
        )
    ).status_code == 403
    assert (
        await client.delete(f"/v1/orgs/{org_id}/members/{b_id}", headers=_auth(a))
    ).status_code == 403


async def test_plain_member_cannot_manage(client: AsyncClient) -> None:
    owner = await _register(client, "owner5@m.com")
    org_id = await _personal_org(client, owner)
    m = await _register(client, "m5@m.com")
    await _add_member(client, owner_token=owner, org_id=org_id, email="m5@m.com", member_token=m)
    other = await _register(client, "other5@m.com")
    other_id = await _add_member(
        client, owner_token=owner, org_id=org_id, email="other5@m.com", member_token=other
    )
    assert (
        await client.patch(
            f"/v1/orgs/{org_id}/members/{other_id}", headers=_auth(m), json={"role": "admin"}
        )
    ).status_code == 403
    assert (
        await client.delete(f"/v1/orgs/{org_id}/members/{other_id}", headers=_auth(m))
    ).status_code == 403


async def test_non_member_masked_and_auth_and_validation(client: AsyncClient) -> None:
    owner = await _register(client, "owner6@m.com")
    org_id = await _personal_org(client, owner)
    owner_id = await _me_id(client, owner)
    outsider = await _register(client, "outsider6@m.com")
    # a non-member cannot read the roster -> 404 mask (not 403, no leak)
    assert (
        await client.get(f"/v1/orgs/{org_id}/members", headers=_auth(outsider))
    ).status_code == 404
    # unauthenticated -> 401
    assert (await client.get(f"/v1/orgs/{org_id}/members")).status_code == 401
    # an invalid role is rejected by the schema -> 422 (cannot set owner via this route)
    assert (
        await client.patch(
            f"/v1/orgs/{org_id}/members/{owner_id}", headers=_auth(owner), json={"role": "owner"}
        )
    ).status_code == 422
