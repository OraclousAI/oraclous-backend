"""Auth journey END-TO-END through the API GATEWAY — NO fakes, zero external dependencies.

Two real users, real JWTs, the real auth-service behind the gateway: an owner registers, invites a
second real user, who registers, peeks the invitation, accepts it, and joins the owner's org; a user
cannot read another org's roster; and anonymous/garbage tokens are rejected at the edge. No OAuth
provider is involved, so this runs fully real on the deployed stack with nothing mocked.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def test_owner_invites_a_second_user_who_accepts_and_joins_the_org(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    owner = register("Owner")
    invitee = register("Invitee")  # a real second user, with their own org

    o = gateway_client(owner["token"])
    assert o.get("/v1/auth/me").json()["org_role"] == "owner"
    members = o.get(f"/v1/orgs/{owner['org_id']}/members").json()
    assert [m["email"] for m in members] == [owner["email"]]  # just the owner to start

    invite = o.post(
        f"/v1/orgs/{owner['org_id']}/invitations",
        json={"email": invitee["email"], "org_role": "member"},
    )
    assert invite.status_code == 201, invite.text
    token = invite.json()["token"]

    # the invitee (a real, logged-in user) peeks the invite — it resolves to the owner's org — then
    # accepts and joins. NOTE: the gateway auths every request, so even peek needs a token at the
    # edge; a logged-in invitee is the real flow.
    i = gateway_client(invitee["token"])
    peek = i.post("/v1/invitations/peek", json={"token": token})
    assert peek.status_code == 200, peek.text
    assert peek.json()["organisation_id"] == owner["org_id"]
    assert i.post("/v1/invitations/accept", json={"token": token}).status_code == 200

    # the owner now sees both members
    emails = {m["email"] for m in o.get(f"/v1/orgs/{owner['org_id']}/members").json()}
    assert emails == {owner["email"], invitee["email"]}


def test_a_user_cannot_read_another_orgs_members_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    org_a = register("Org A")
    org_b = register("Org B")
    b = gateway_client(org_b["token"])
    # B has no membership in A's org → the gateway must not expose A's roster
    assert b.get(f"/v1/orgs/{org_a['org_id']}/members").status_code in (403, 404)


def test_login_is_required_no_anonymous_access_through_the_gateway(
    register: Callable[..., dict], gateway_url: str
) -> None:
    user = register("Anon Check")
    # a valid token reaches /me; a missing/garbage token is rejected at the edge
    ok = httpx.get(
        f"{gateway_url}/v1/auth/me",
        headers={"Authorization": f"Bearer {user['token']}"},
        timeout=15.0,
    )
    assert ok.status_code == 200
    assert httpx.get(f"{gateway_url}/v1/auth/me", timeout=15.0).status_code == 401
    assert (
        httpx.get(
            f"{gateway_url}/v1/auth/me",
            headers={"Authorization": "Bearer garbage"},
            timeout=15.0,
        ).status_code
        == 401
    )
