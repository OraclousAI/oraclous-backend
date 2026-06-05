"""KRS gateway-mode auth (ADR-018 edge-auth): the service trusts the gateway's verified identity
headers and validates NO token. Identity comes from X-Principal-*/X-Organisation-Id; the request is
gated on the shared X-Internal-Key (fail-closed). No DB / no network.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from oraclous_governance import PrincipalType
from oraclous_knowledge_retriever_service.core.auth import AuthError, principal_from_gateway_headers
from oraclous_knowledge_retriever_service.core.config import get_settings
from oraclous_knowledge_retriever_service.core.dependencies import _require_internal_key

pytestmark = pytest.mark.unit

_USER = "11111111-1111-1111-1111-111111111111"
_ORG = "22222222-2222-2222-2222-222222222222"
_KEY = "test-internal-key"  # noqa: S105 — test attestation key


@pytest.fixture(autouse=True)
def _gw_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KRS_AUTH_MODE", "gateway")
    monkeypatch.setenv("KRS_INTERNAL_SERVICE_KEY", _KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_principal_from_valid_gateway_headers() -> None:
    principal = principal_from_gateway_headers(_USER, "user", _ORG)
    assert str(principal.principal_id) == _USER
    assert principal.principal_type == PrincipalType.USER
    assert principal.organisation_id is not None and str(principal.organisation_id) == _ORG


def test_rejects_missing_identity_headers() -> None:
    with pytest.raises(AuthError):
        principal_from_gateway_headers(None, "user", _ORG)
    with pytest.raises(AuthError):
        principal_from_gateway_headers(_USER, None, _ORG)


def test_rejects_malformed_identity_headers() -> None:
    with pytest.raises(AuthError):
        principal_from_gateway_headers("not-a-uuid", "user", _ORG)


def test_internal_key_gate_accepts_the_shared_key() -> None:
    _require_internal_key(_KEY)  # must not raise


def test_internal_key_gate_rejects_missing_or_wrong_key() -> None:
    with pytest.raises(HTTPException) as missing:
        _require_internal_key(None)
    assert missing.value.status_code == 403
    with pytest.raises(HTTPException) as wrong:
        _require_internal_key("wrong-key")
    assert wrong.value.status_code == 403
