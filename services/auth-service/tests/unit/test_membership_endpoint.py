"""#464: the internal grantee-membership endpoint — GET /internal/v1/orgs/{org}/members/{user}.

Internal-key-gated (no caller-membership gate — a service-to-service check KGS's grant path calls);
returns {"is_member": bool} from OrgMemberRepository. RED until the endpoint exists.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_INTERNAL_KEY = "internal-service-key-for-tests"
_ORG = "org-Y-1234"
_MEMBER = "user-in-org-5678"
_STRANGER = "user-not-in-org-9999"


class _FakeResult:
    def __init__(self, member: object) -> None:
        self._member = member

    def scalar_one_or_none(self) -> object:
        return self._member


class _FakeSession:
    """A minimal AsyncSession stand-in: ``execute`` returns whatever OrgMemberRepository.get expects
    (a result whose ``scalar_one_or_none`` is the OrgMember for a member, else None)."""

    def __init__(self, member: object) -> None:
        self._member = member

    async def execute(self, *_a: object, **_k: object) -> _FakeResult:
        return _FakeResult(self._member)


@pytest.fixture
def client() -> Iterator[TestClient]:
    from oraclous_auth_service.app import create_app

    class _FakeAgentRepo:  # the endpoint under test doesn't touch it; create_app requires it
        pass

    app = create_app(agent_repository=_FakeAgentRepo(), internal_service_key=_INTERNAL_KEY)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _override_membership(app: object, *, member: object) -> None:
    from oraclous_auth_service.core.dependencies import get_session

    async def _fake_session() -> object:  # yield a session whose repo read returns `member`
        yield _FakeSession(member)

    app.dependency_overrides[get_session] = _fake_session  # type: ignore[attr-defined]


def test_member_is_reported_true(client: TestClient) -> None:
    _override_membership(client.app, member=object())  # a non-None OrgMember row
    r = client.get(
        f"/internal/v1/orgs/{_ORG}/members/{_MEMBER}", headers={"X-Internal-Key": _INTERNAL_KEY}
    )
    assert r.status_code == 200 and r.json() == {"is_member": True}


def test_non_member_is_reported_false(client: TestClient) -> None:
    _override_membership(client.app, member=None)  # no OrgMember row → not a member
    r = client.get(
        f"/internal/v1/orgs/{_ORG}/members/{_STRANGER}", headers={"X-Internal-Key": _INTERNAL_KEY}
    )
    assert r.status_code == 200 and r.json() == {"is_member": False}


def test_the_endpoint_requires_the_internal_key(client: TestClient) -> None:
    _override_membership(client.app, member=object())
    # no key → 401 (fail-closed); a wrong key → 401 — never a 200 that could be trusted
    assert client.get(f"/internal/v1/orgs/{_ORG}/members/{_MEMBER}").status_code == 401
    r = client.get(
        f"/internal/v1/orgs/{_ORG}/members/{_MEMBER}", headers={"X-Internal-Key": "wrong"}
    )
    assert r.status_code == 401
