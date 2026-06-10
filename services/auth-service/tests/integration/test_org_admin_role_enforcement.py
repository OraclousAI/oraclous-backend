"""Integration: org-admin role gate admits a promoted ADMIN, forbids a plain MEMBER (#233).

The existing invitation suite proves owner-can / member-cannot, but never the case the gate is
FOR: a non-owner who has been PROMOTED to ``admin`` can perform an admin-gated action that a plain
``member`` in the SAME org is forbidden (403) from. This closes that gap end-to-end vs Postgres:

  - owner registers, promotes Bob to ``admin`` and leaves Carol a plain ``member``;
  - the promoted ADMIN (not the owner) can create an invitation AND list the roster (admin-gated
    actions succeed for the ``admin`` rank, not only the owner);
  - the plain MEMBER is forbidden (403) from the same invitation-create action.

Uses the shared ``client`` fixture (conftest) — real SQL below the route.
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


async def test_promoted_admin_can_invite_but_plain_member_cannot(client: AsyncClient) -> None:
    owner = await _register(client, "owner-rbac@ex.com")
    org_id = await _personal_org(client, owner)

    # Bob joins, then is promoted by the owner to admin.
    bob = await _register(client, "bob-rbac@ex.com")
    bob_id = await _add_member(
        client, owner_token=owner, org_id=org_id, email="bob-rbac@ex.com", member_token=bob
    )
    promoted = await client.patch(
        f"/v1/orgs/{org_id}/members/{bob_id}", headers=_auth(owner), json={"role": "admin"}
    )
    assert promoted.status_code == 200 and promoted.json()["role"] == "admin", promoted.text

    # Carol joins and stays a plain member.
    carol = await _register(client, "carol-rbac@ex.com")
    await _add_member(
        client, owner_token=owner, org_id=org_id, email="carol-rbac@ex.com", member_token=carol
    )

    # the PROMOTED ADMIN (not the owner) can create an invitation — the admin rank passes the gate
    admin_invite = await client.post(
        f"/v1/orgs/{org_id}/invitations", headers=_auth(bob), json={"email": "dave-rbac@ex.com"}
    )
    assert admin_invite.status_code == 201, admin_invite.text

    # the plain MEMBER is forbidden (403) from the same admin-gated action
    member_invite = await client.post(
        f"/v1/orgs/{org_id}/invitations", headers=_auth(carol), json={"email": "eve-rbac@ex.com"}
    )
    assert member_invite.status_code == 403, member_invite.text


async def test_promoted_admin_can_list_invitations_member_cannot(client: AsyncClient) -> None:
    owner = await _register(client, "owner-rbac2@ex.com")
    org_id = await _personal_org(client, owner)

    bob = await _register(client, "bob-rbac2@ex.com")
    bob_id = await _add_member(
        client, owner_token=owner, org_id=org_id, email="bob-rbac2@ex.com", member_token=bob
    )
    await client.patch(
        f"/v1/orgs/{org_id}/members/{bob_id}", headers=_auth(owner), json={"role": "admin"}
    )

    carol = await _register(client, "carol-rbac2@ex.com")
    await _add_member(
        client, owner_token=owner, org_id=org_id, email="carol-rbac2@ex.com", member_token=carol
    )

    # an admin-gated read (list invitations) succeeds for the promoted admin
    admin_list = await client.get(f"/v1/orgs/{org_id}/invitations", headers=_auth(bob))
    assert admin_list.status_code == 200, admin_list.text

    # but is forbidden (403) for the plain member
    member_list = await client.get(f"/v1/orgs/{org_id}/invitations", headers=_auth(carol))
    assert member_list.status_code == 403, member_list.text
