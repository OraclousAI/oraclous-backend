"""Integration: /internal/resolve-credential returns a stored secret to a trusted service (S5a).

A connection_string credential is created via the org-scoped CRUD, then resolved by id over the
internal (X-Internal-Key) endpoint → the decrypted payload comes back. Missing key → 401, unknown id
→ 404, cross-org id → 404 (mask). Key-free (dev ENCRYPTION_KEY + dev bearer + testcontainer PG).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_DEV_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # noqa: S105 — 32-byte dev key
_INTERNAL = "s5a-internal-key"  # noqa: S105 — test internal key
_DEV_ORG = "00000000-0000-0000-0000-00000000050a"
_OTHER_ORG = "00000000-0000-0000-0000-0000000006ff"


@pytest.fixture
async def client(
    postgres_dsn: str, test_envelope, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    monkeypatch.setenv("DATABASE_URL", async_dsn)
    monkeypatch.setenv("ENCRYPTION_KEY", _DEV_KEY)
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", _INTERNAL)
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("DEV_BEARER", "dev-token")
    monkeypatch.setenv("DEV_ORG_ID", _DEV_ORG)
    from oraclous_credential_broker_service.core.config import get_settings

    get_settings.cache_clear()

    from oraclous_credential_broker_service.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    setup_engine = create_async_engine(async_dsn)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    from oraclous_credential_broker_service.app.factory import create_app
    from oraclous_credential_broker_service.repositories.credential_repository import (
        CredentialRepository,
    )

    app = create_app(lifespan=None)
    repo = CredentialRepository(async_dsn, encrypt=test_envelope.encrypt)
    app.state.credential_repository = repo
    app.state.envelope_service = test_envelope
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cb.test") as c:
        yield c
    await repo.close()
    get_settings.cache_clear()


def _bearer() -> dict:
    return {"Authorization": "Bearer dev-token", "Content-Type": "application/json"}


def _internal(key: str = _INTERNAL) -> dict:
    return {"X-Internal-Key": key, "Content-Type": "application/json"}


async def _create_conn_string(client: AsyncClient) -> str:
    body = {
        "tool_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "name": "pg",
        "provider": "postgresql",
        "cred_type": "raw",
        "credential": {"connection_string": "postgresql://h:5432/db"},
    }
    created = await client.post("/credentials/", json=body, headers=_bearer())
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def test_resolve_returns_decrypted_payload(client: AsyncClient) -> None:
    cred_id = await _create_conn_string(client)
    resp = await client.post(
        "/internal/resolve-credential",
        json={"organisation_id": _DEV_ORG, "credential_id": cred_id},
        headers=_internal(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["credential"] == {"connection_string": "postgresql://h:5432/db"}
    assert body["provider"] == "postgresql"


async def test_missing_internal_key_is_401(client: AsyncClient) -> None:
    cred_id = await _create_conn_string(client)
    resp = await client.post(
        "/internal/resolve-credential",
        json={"organisation_id": _DEV_ORG, "credential_id": cred_id},
    )
    assert resp.status_code == 401


async def test_unknown_id_is_404(client: AsyncClient) -> None:
    resp = await client.post(
        "/internal/resolve-credential",
        json={"organisation_id": _DEV_ORG, "credential_id": str(uuid.uuid4())},
        headers=_internal(),
    )
    assert resp.status_code == 404


async def test_cross_org_is_404(client: AsyncClient) -> None:
    cred_id = await _create_conn_string(client)
    resp = await client.post(
        "/internal/resolve-credential",
        json={"organisation_id": _OTHER_ORG, "credential_id": cred_id},
        headers=_internal(),
    )
    assert resp.status_code == 404
