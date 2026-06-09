"""Unit: the access JWT carries the member's org_role claim (R7-SEC S2). No DB."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

os.environ.setdefault("JWT_SECRET", "test-secret-for-r7-sec-not-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")

_UID = "00000000-0000-0000-0000-0000000000e6"
_OID = "00000000-0000-0000-0000-00000000050a"


def test_access_token_stamps_the_org_role_claim() -> None:
    from oraclous_auth_service.core.jwt_handler import create_user_token, decode_token

    token, _ = create_user_token(
        user_id=_UID, organisation_id=_OID, email="a@x.io", org_role="admin"
    )
    claims = decode_token(token)
    assert claims["org_role"] == "admin"
    assert claims["organisation_id"] == _OID and claims["type"] == "access"


def test_org_role_is_omitted_when_unknown() -> None:
    # a None role mints NO org_role claim, so the gateway reads it as None (never an admin) — a
    # non-member / pre-claim token can't satisfy an admin gate by accident.
    from oraclous_auth_service.core.jwt_handler import create_user_token, decode_token

    token, _ = create_user_token(user_id=_UID, organisation_id=_OID, email="a@x.io")
    assert "org_role" not in decode_token(token)
