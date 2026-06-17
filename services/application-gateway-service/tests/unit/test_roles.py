"""Unit: the org-admin roles floor — require_admin PDP (R7-SEC S2)."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from oraclous_application_gateway_service.core.dependencies import require_admin
from oraclous_governance import Principal, PrincipalType

pytestmark = [pytest.mark.unit, pytest.mark.api_authz]


def _p(role: str | None) -> Principal:
    return Principal(
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
        organisation_id=uuid.uuid4(),
        org_role=role,
    )


@pytest.mark.parametrize("role", ["admin", "owner"])
async def test_require_admin_allows_admin_and_owner(role: str) -> None:
    out = await require_admin(_p(role))
    assert out.org_role == role


@pytest.mark.parametrize("role", ["member", None, "viewer", ""])
async def test_require_admin_denies_member_and_unknown_roles(role: str | None) -> None:
    # fail-closed: a member, a roleless token (None — e.g. minted before the claim existed), and any
    # unknown role are all denied the destructive management ops.
    with pytest.raises(HTTPException) as exc:
        await require_admin(_p(role))
    assert exc.value.status_code == 403
