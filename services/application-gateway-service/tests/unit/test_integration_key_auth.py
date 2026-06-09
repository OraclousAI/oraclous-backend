"""Unit: the integration-key validator (resolve_principal) — fail-closed on every bad key."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from oraclous_application_gateway_service.core.auth import AuthError
from oraclous_application_gateway_service.domain.integration_key import mint_key
from oraclous_application_gateway_service.services.integration_key_auth_service import (
    IntegrationKeyAuthService,
)
from oraclous_governance import PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()


def _row(minted, *, status="active", expires_at=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        key_hash=minted.key_hash,
        status=status,
        expires_at=expires_at,
        bound_agent_slug=None,
        capability_allow_list=None,
        cors_origins=None,
    )


class _FakeRepo:
    def __init__(self, row=None) -> None:
        self._row = row

    async def get_by_prefix(self, key_prefix):  # noqa: ANN001
        return self._row


async def test_valid_key_resolves_to_a_scoped_service_account() -> None:
    minted = mint_key("oak")
    svc = IntegrationKeyAuthService(_FakeRepo(_row(minted)))
    principal = await svc.resolve_principal(minted.plaintext)
    assert principal.principal_type == PrincipalType.SERVICE_ACCOUNT
    assert principal.organisation_id == _ORG  # the key's own org, never None


async def test_unknown_prefix_fails_closed() -> None:
    svc = IntegrationKeyAuthService(_FakeRepo(None))
    with pytest.raises(AuthError):
        await svc.resolve_principal(mint_key().plaintext)


async def test_wrong_secret_fails_closed() -> None:
    real, attacker = mint_key("oak"), mint_key("oak")
    # the row stores the REAL key's hash; the attacker presents a different secret
    svc = IntegrationKeyAuthService(_FakeRepo(_row(real)))
    with pytest.raises(AuthError):
        await svc.resolve_principal(attacker.plaintext)


async def test_revoked_key_fails_closed() -> None:
    minted = mint_key("oak")
    svc = IntegrationKeyAuthService(_FakeRepo(_row(minted, status="revoked")))
    with pytest.raises(AuthError, match="revoked"):
        await svc.resolve_principal(minted.plaintext)


async def test_expired_key_fails_closed() -> None:
    minted = mint_key("oak")
    past = datetime.now(UTC) - timedelta(seconds=1)
    svc = IntegrationKeyAuthService(_FakeRepo(_row(minted, expires_at=past)))
    with pytest.raises(AuthError, match="expired"):
        await svc.resolve_principal(minted.plaintext)


async def test_unexpired_ttl_is_accepted() -> None:
    minted = mint_key("oak")
    future = datetime.now(UTC) + timedelta(hours=1)
    svc = IntegrationKeyAuthService(_FakeRepo(_row(minted, expires_at=future)))
    principal = await svc.resolve_principal(minted.plaintext)
    assert principal.organisation_id == _ORG


async def test_malformed_key_fails_closed() -> None:
    svc = IntegrationKeyAuthService(_FakeRepo(None))
    with pytest.raises(AuthError, match="malformed"):
        await svc.resolve_principal("oak-short")
