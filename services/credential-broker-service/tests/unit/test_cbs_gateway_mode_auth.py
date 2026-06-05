"""CBS gateway-mode edge auth (ADR-018): the user-facing edge trusts the gateway's verified
X-Organisation-Id and validates NO token (the X-Internal-Key gate is the existing
verify_internal_key, covered by test_internal_service_key). Covers the org-from-header builder.
"""

from __future__ import annotations

import pytest
from oraclous_credential_broker_service.core.auth import (
    AuthError,
    organisation_id_from_gateway_headers,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

_ORG = "22222222-2222-2222-2222-222222222222"


def test_org_from_valid_gateway_header() -> None:
    assert str(organisation_id_from_gateway_headers(_ORG)) == _ORG


def test_rejects_missing_org_header() -> None:
    with pytest.raises(AuthError):
        organisation_id_from_gateway_headers(None)


def test_rejects_malformed_org_header() -> None:
    with pytest.raises(AuthError):
        organisation_id_from_gateway_headers("not-a-uuid")
