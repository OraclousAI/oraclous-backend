"""Unit: the shared org-role rank predicate + Principal.org_role (R7-SEC S2)."""

from __future__ import annotations

import uuid

import pytest
from oraclous_governance import Principal, PrincipalType, org_role_at_least

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("role", "minimum", "expected"),
    [
        ("owner", "admin", True),
        ("admin", "admin", True),
        ("owner", "member", True),
        ("member", "member", True),
        ("member", "admin", False),  # the headline floor: a member is not an admin
        (None, "admin", False),  # roleless -> below member (fail-closed)
        ("", "admin", False),
        ("superuser", "admin", False),  # unknown role -> below member
        ("admin", "galaxy-brain", False),  # unknown minimum -> unreachable (fail-closed)
    ],
)
def test_org_role_at_least(role: str | None, minimum: str, expected: bool) -> None:
    assert org_role_at_least(role, minimum=minimum) is expected


def test_principal_carries_an_optional_org_role() -> None:
    p = Principal(principal_id=uuid.uuid4(), principal_type=PrincipalType.USER, org_role="admin")
    assert p.org_role == "admin"
    # default is None for non-member principals (agent / service-account / pre-claim tokens)
    assert Principal(principal_id=uuid.uuid4(), principal_type=PrincipalType.AGENT).org_role is None
