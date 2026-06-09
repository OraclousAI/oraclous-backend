"""Unit: WebhookSecretService — mint encrypts, resolve decrypts, org-scoped not-found. No DB."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_DEV_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # noqa: S105 — 32-byte test key
_ORG = uuid.uuid4()


@pytest.fixture(autouse=True)
def _enc_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENCRYPTION_KEY", _DEV_KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "k")
    from oraclous_credential_broker_service.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeRepo:
    def __init__(self) -> None:
        self.rows: list = []

    async def create(self, *, organisation_id, encrypted_secret):  # noqa: ANN001
        row = SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            encrypted_secret=encrypted_secret,
            status="active",
        )
        self.rows.append(row)
        return row

    async def get_for_org(self, *, secret_id, organisation_id):  # noqa: ANN001
        return next(
            (r for r in self.rows if r.id == secret_id and r.organisation_id == organisation_id),
            None,
        )


async def test_mint_then_resolve_round_trips_the_secret() -> None:
    from oraclous_credential_broker_service.services.webhook_secret_service import (
        WebhookSecretService,
    )

    repo = _FakeRepo()
    svc = WebhookSecretService(repo)
    sid = await svc.mint(organisation_id=_ORG, secret="super-secret-hmac-key")  # noqa: S106
    # stored ciphertext is NOT the plaintext (encrypted at rest)
    assert repo.rows[0].encrypted_secret != "super-secret-hmac-key"  # noqa: S105
    # resolve in the same org returns the plaintext
    assert await svc.resolve(secret_id=sid, organisation_id=_ORG) == "super-secret-hmac-key"


async def test_cross_org_resolve_is_not_found() -> None:
    from oraclous_credential_broker_service.services.webhook_secret_service import (
        WebhookSecretNotFound,
        WebhookSecretService,
    )

    svc = WebhookSecretService(_FakeRepo())
    sid = await svc.mint(organisation_id=_ORG, secret="s")  # noqa: S106
    with pytest.raises(WebhookSecretNotFound):
        await svc.resolve(secret_id=sid, organisation_id=uuid.uuid4())  # another org


async def test_unknown_id_is_not_found() -> None:
    from oraclous_credential_broker_service.services.webhook_secret_service import (
        WebhookSecretNotFound,
        WebhookSecretService,
    )

    with pytest.raises(WebhookSecretNotFound):
        await WebhookSecretService(_FakeRepo()).resolve(
            secret_id=uuid.uuid4(), organisation_id=_ORG
        )
