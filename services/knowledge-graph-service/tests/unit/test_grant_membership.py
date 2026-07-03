"""#464: the cross-org grant verifies grantee ∈ grantee-org before writing the edge (fail-closed).

Unit — the membership gate + the auth-membership client. The non-member/unreachable paths reject
BEFORE any ReBAC write (a fake engine asserts it is never called); the member happy-path (real ReBAC
writes) is proven end-to-end in the deployed e2e.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from oraclous_knowledge_graph_service.services.auth_client import (
    AuthServiceUnavailable,
    FakeAuthClient,
    RealAuthClient,
)
from oraclous_knowledge_graph_service.services.grant_service import (
    GranteeNotInOrg,
    GraphGrantService,
)

pytestmark = pytest.mark.unit

_GRAPH = uuid.uuid4()
_OWNER = uuid.uuid4()
_GORG = uuid.uuid4()
_GUSER = uuid.uuid4()


class _FakeGraphs:
    async def assert_owned(self, *, graph_id: uuid.UUID, user_id: uuid.UUID) -> None:
        return None  # the owner gate passes — this test is about the #464 grantee-membership gate


class _NeverEngine:
    """A ReBAC engine whose write methods MUST NOT be called — proves the membership gate rejects
    before any edge is written."""

    async def bootstrap_graph_roles(self, *a: object, **k: object) -> None:
        raise AssertionError("no ReBAC write may happen when the grantee-membership gate rejects")

    grant_role = invalidate_permission_cache = bootstrap_graph_roles


def _svc(auth: object) -> GraphGrantService:
    return GraphGrantService(
        graphs=_FakeGraphs(), engine=_NeverEngine(), async_driver=object(), auth=auth
    )


async def test_fake_auth_client_reports_membership_by_pair() -> None:
    client = FakeAuthClient(members={(str(_GORG), str(_GUSER))})
    assert await client.verify_membership(organisation_id=str(_GORG), user_id=str(_GUSER)) is True
    other = str(uuid.uuid4())
    assert await client.verify_membership(organisation_id=str(_GORG), user_id=other) is False


async def test_real_auth_client_returns_is_member_and_fails_closed_on_errors() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/members/member"):
            return httpx.Response(200, json={"is_member": True})
        if req.url.path.endswith("/members/nonmember"):
            return httpx.Response(200, json={"is_member": False})
        if req.url.path.endswith("/members/boom"):
            return httpx.Response(500, text="db down")
        if req.url.path.endswith("/members/garbled"):
            return httpx.Response(200, text="not json")  # a malformed 200 (review M2)
        raise httpx.ConnectError("unreachable")

    client = RealAuthClient(
        base_url="http://auth:8000", internal_key="k", transport=httpx.MockTransport(handler)
    )
    assert await client.verify_membership(organisation_id="o", user_id="member") is True
    assert await client.verify_membership(organisation_id="o", user_id="nonmember") is False
    with pytest.raises(AuthServiceUnavailable):  # a 500 is fail-closed, NOT "not a member"
        await client.verify_membership(organisation_id="o", user_id="boom")
    with pytest.raises(AuthServiceUnavailable):  # a transport error is fail-closed
        await client.verify_membership(organisation_id="o", user_id="unreachable")
    with pytest.raises(AuthServiceUnavailable):  # a malformed 200 body is fail-closed, not a 500
        await client.verify_membership(organisation_id="o", user_id="garbled")


async def test_grant_read_rejects_a_grantee_who_is_not_in_the_grantee_org() -> None:
    svc = _svc(FakeAuthClient(members=set()))  # the grantee is NOT a member of the grantee org
    with pytest.raises(GranteeNotInOrg):  # 422 at the route; _NeverEngine proves no edge written
        await svc.grant_read(
            graph_id=_GRAPH,
            owner_user_id=_OWNER,
            grantee_organisation_id=_GORG,
            grantee_user_id=_GUSER,
        )


async def test_grant_read_fail_closes_when_auth_service_is_unreachable() -> None:
    class _Down:
        async def verify_membership(self, **_k: object) -> bool:
            raise AuthServiceUnavailable("down")

        async def aclose(self) -> None: ...

    with pytest.raises(AuthServiceUnavailable):  # 503 at the route — never grant un-validated
        await _svc(_Down()).grant_read(
            graph_id=_GRAPH,
            owner_user_id=_OWNER,
            grantee_organisation_id=_GORG,
            grantee_user_id=_GUSER,
        )
