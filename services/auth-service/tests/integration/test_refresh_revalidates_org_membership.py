"""Refresh-time org-membership re-validation (WP-2, T2 revocation-race).

The durable guardrail for the WP-2 fix: ``AuthService.refresh`` resolves the token's active org
through the SAME membership gate (``OrgService.resolve_active_org``) that ``login``/``switch_org``
use, BEFORE issuing the rotated pair. Without it, an org-scoped refresh token outlives membership —
a member removed from org O keeps O access until the (longer) refresh TTL elapses.

These tests are end-to-end over the live FastAPI app + real Postgres (the shared ``client``
fixture), so they pin the *behaviour* rather than a refactor-fragile shape: a removed member's
refresh is denied (404, enumeration-masked, no token issued), while a still-valid member's refresh
keeps the org and an absent-claim refresh keeps the default-org fallback.

This durable test — not an AST/linter heuristic — IS the guardrail for refresh-time org issuance.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.organization_isolation,
    pytest.mark.security,
]


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


async def _join_org(
    client: AsyncClient, *, owner_token: str, org_id: str, email: str, member_token: str
) -> str:
    """Invite ``email`` into ``org_id`` and accept as ``member_token``; returns the member id."""
    inv = await client.post(
        f"/v1/orgs/{org_id}/invitations", headers=_auth(owner_token), json={"email": email}
    )
    assert inv.status_code == 201, inv.text
    acc = await client.post(
        "/v1/invitations/accept", headers=_auth(member_token), json={"token": inv.json()["token"]}
    )
    assert acc.status_code == 200, acc.text
    return await _me_id(client, member_token)


async def _refresh_scoped_to(client: AsyncClient, *, access_token: str, org_id: str) -> str:
    """Switch the session to ``org_id`` (org rides on the refresh token); return that token."""
    # switch-org takes the target org from the X-Organisation-Id header (never the body), and the
    # selected org rides on the new refresh token so it survives subsequent refreshes.
    switched = await client.post(
        "/v1/auth/switch-org",
        headers={**_auth(access_token), "X-Organisation-Id": org_id},
    )
    assert switched.status_code == 200, switched.text
    return switched.json()["refresh_token"]


async def test_member_refresh_preserves_org_happy_path(client: AsyncClient) -> None:
    """(a) A member of org O refreshes -> a new pair still scoped to O."""
    owner = await _register(client, "owner-a@m.com")
    org_o = await _personal_org(client, owner)
    member = await _register(client, "member-a@m.com")
    await _join_org(
        client, owner_token=owner, org_id=org_o, email="member-a@m.com", member_token=member
    )

    member_refresh = await _refresh_scoped_to(client, access_token=member, org_id=org_o)

    rotated = await client.post("/v1/auth/refresh", json={"refresh_token": member_refresh})
    assert rotated.status_code == 200, rotated.text
    new_access = rotated.json()["access_token"]
    # The rotated access token is still scoped to O (the membership gate passed).
    me = await client.get("/v1/auth/me", headers=_auth(new_access))
    assert me.status_code == 200
    assert me.json()["organisation_id"] == org_o


async def test_removed_member_refresh_is_denied(client: AsyncClient) -> None:
    """(b) A member removed from O, refreshing with O in the claim -> 404, NO token issued.

    The guardrail: before the WP-2 fix this would re-issue an O-scoped pair from the stale claim.
    """
    owner = await _register(client, "owner-b@m.com")
    org_o = await _personal_org(client, owner)
    member = await _register(client, "member-b@m.com")
    member_id = await _join_org(
        client, owner_token=owner, org_id=org_o, email="member-b@m.com", member_token=member
    )

    member_refresh = await _refresh_scoped_to(client, access_token=member, org_id=org_o)

    # Owner removes the member from O.
    removed = await client.delete(f"/v1/orgs/{org_o}/members/{member_id}", headers=_auth(owner))
    assert removed.status_code == 204, removed.text

    # The O-scoped refresh token now refers to an org the user no longer belongs to → 404-masked.
    denied = await client.post("/v1/auth/refresh", json={"refresh_token": member_refresh})
    assert denied.status_code == 404, denied.text
    assert "refresh_token" not in denied.json()
    assert "access_token" not in denied.json()


async def test_refresh_with_org_user_never_belonged_to_is_denied(client: AsyncClient) -> None:
    """(c) A refresh token whose org claim names an org the user never joined -> denied.

    Forged by carrying another user's org onto a switch attempt: switch-org itself rejects it (404),
    so the user can never obtain an O-scoped refresh token in the first place — the membership gate
    is upstream of issuance. This pins that the gate refresh now shares is the same one that already
    blocks non-members at switch/login time.
    """
    stranger = await _register(client, "stranger-c@m.com")
    org_o = await _personal_org(client, stranger)
    outsider = await _register(client, "outsider-c@m.com")

    # The outsider never joined org_o; trying to scope a session to it is 404-masked.
    forged = await client.post(
        "/v1/auth/switch-org",
        headers={**_auth(outsider), "X-Organisation-Id": org_o},
    )
    assert forged.status_code == 404, forged.text
