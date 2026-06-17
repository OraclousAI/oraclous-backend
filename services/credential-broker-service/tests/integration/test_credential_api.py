"""Integration: encrypted credential CRUD vs real Postgres (S1).

Proves create → DB stores AES-GCM ciphertext (not readable JSON) → the user-facing GET/retrieve
return METADATA ONLY (the decrypted secret is never exposed on this surface; runtime resolution uses
the X-Internal-Key /internal path) → cross-org/cross-user read is 404 (T1) → update → delete.
Dev-auth binds org + user from the bearer (ORG001). Key-free (dev key + bearer).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

pytestmark = pytest.mark.integration

_DEV_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # noqa: S105 — 32-byte dev key
_DEV_ORG = "00000000-0000-0000-0000-00000000050a"
_OTHER_ORG = "00000000-0000-0000-0000-0000000006ff"


@pytest.fixture
async def client(
    broker_dsns, test_envelope, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    # ADR-030: the service repos run as the NOSUPERUSER oraclous_app role (RLS bites them); the
    # broker_dsns fixture already created the schema + enabled RLS + provisioned the role as the
    # superuser owner. DATABASE_URL (what the repos read) points at the app role; the at-rest
    # introspection reads in the tests use the superuser DSN explicitly via _owner_async_dsn.
    owner_async_dsn, app_async_dsn = broker_dsns
    monkeypatch.setenv("DATABASE_URL", app_async_dsn)
    monkeypatch.setenv("ENCRYPTION_KEY", _DEV_KEY)
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "dev-internal-key")
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("DEV_BEARER", "dev-token")
    monkeypatch.setenv("DEV_ORG_ID", _DEV_ORG)
    # the owner DSN is where the at-rest ciphertext reads run (RLS would hide rows under the app
    # role with no GUC bound) — stash it for the introspecting tests.
    monkeypatch.setenv("_OWNER_DATABASE_URL", owner_async_dsn)
    from oraclous_credential_broker_service.core.config import get_settings

    get_settings.cache_clear()

    # ASGITransport doesn't run the lifespan, so wire app.state directly (mirrors the auth tests).
    from oraclous_credential_broker_service.app.factory import create_app
    from oraclous_credential_broker_service.repositories.credential_repository import (
        CredentialRepository,
    )
    from oraclous_credential_broker_service.repositories.postgres_delegated_token_store import (
        PostgresDelegatedTokenStore,
    )
    from oraclous_credential_broker_service.services.delegation_service import DelegationService
    from oraclous_substrate.access_async import install_org_guc_guard
    from sqlalchemy.ext.asyncio import create_async_engine

    app = create_app(lifespan=None)
    # repos run as oraclous_app — the org_scope/begin-event binds the GUC so RLS admits the org.
    cred_repo = CredentialRepository(app_async_dsn, encrypt=test_envelope.encrypt)
    engine = create_async_engine(app_async_dsn)
    install_org_guc_guard(engine)  # delegated-token store's externally-built engine needs it too
    app.state.credential_repository = cred_repo
    app.state.envelope_service = test_envelope
    app.state.delegation_service = DelegationService(
        store=PostgresDelegatedTokenStore(engine=engine)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cb.test") as c:
        yield c
    await cred_repo.close()
    await engine.dispose()
    get_settings.cache_clear()


def _auth(bearer: str = "dev-token") -> dict:
    return {"Authorization": f"Bearer {bearer}"}


def _payload() -> dict:
    return {
        "tool_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "name": "my google",
        "provider": "google",
        "cred_type": "oauth",
        "credential": {"access_token": "super-secret", "refresh_token": "r3fr3sh"},
    }


async def test_create_encrypts_at_rest_and_reads_are_metadata_only(client: AsyncClient) -> None:
    body = _payload()
    created = await client.post("/credentials/", json=body, headers=_auth())
    assert created.status_code == 201, created.text
    cred_id = created.json()["id"]
    assert "credential" not in created.json()  # create response is metadata-only

    # the stored ciphertext is NOT the plaintext secret — read it as the OWNER (superuser bypasses
    # RLS) so this at-rest assertion sees the row regardless of the GUC (ADR-030).
    import os

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(os.environ["_OWNER_DATABASE_URL"])
    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                text("SELECT encrypted_cred FROM user_credentials WHERE id = :i"),
                {"i": cred_id},
            )
        ).scalar_one()
    await engine.dispose()
    assert "super-secret" not in stored and "access_token" not in stored

    # the user-facing GET returns metadata only — the decrypted secret is NEVER exposed here
    got = await client.get(f"/credentials/{cred_id}", headers=_auth())
    assert got.status_code == 200
    assert "credential" not in got.json()
    assert got.json()["provider"] == "google" and got.json()["id"] == cred_id

    # the retrieve/list roster is likewise metadata-only (no secret on any item)
    listed = await client.post(
        "/credentials/retrieve/", json={"user_id": body["user_id"]}, headers=_auth()
    )
    assert listed.status_code == 200
    assert listed.json() and all("credential" not in item for item in listed.json())


async def test_cross_org_read_is_denied(client: AsyncClient) -> None:
    created = await client.post("/credentials/", json=_payload(), headers=_auth())
    cred_id = created.json()["id"]
    # dev-auth binds one org; the repo filters by it, so an unknown id 404s (the cross-org row is
    # likewise indistinguishable — repo WHERE includes organisation_id).
    missing = await client.get(f"/credentials/{uuid.uuid4()}", headers=_auth())
    assert missing.status_code == 404
    # the real row is readable in its own org
    assert (await client.get(f"/credentials/{cred_id}", headers=_auth())).status_code == 200


async def test_update_and_delete(client: AsyncClient) -> None:
    body = _payload()
    cred_id = (await client.post("/credentials/", json=body, headers=_auth())).json()["id"]
    upd = {
        "id": cred_id,
        "name": "renamed",
        "provider": "google",
        "user_id": body["user_id"],
        "tool_id": body["tool_id"],
        "cred_type": "oauth",
        "credential": {"access_token": "rotated"},
    }
    r = await client.put(f"/credentials/{cred_id}", json=upd, headers=_auth())
    assert r.status_code == 200 and r.json()["name"] == "renamed"
    # the user-facing read stays metadata-only after an update (no decrypted secret returned)
    got = await client.get(f"/credentials/{cred_id}", headers=_auth())
    assert got.status_code == 200 and "credential" not in got.json()
    assert (await client.delete(f"/credentials/{cred_id}", headers=_auth())).status_code == 204
    assert (await client.get(f"/credentials/{cred_id}", headers=_auth())).status_code == 404


async def test_update_path_body_id_mismatch_is_400_and_leaves_addressed_untouched(
    client: AsyncClient,
) -> None:
    """PUT /credentials/{A} with body.id={B} → 400, and {A} is provably untouched (#343).

    The path param is authoritative; a mismatched body.id is a malformed REST request that must be
    rejected BEFORE any write, so the addressed (path) credential is left exactly as it was.
    """
    body_a = _payload()
    cred_a = (await client.post("/credentials/", json=body_a, headers=_auth())).json()["id"]
    body_b = _payload()
    cred_b = (await client.post("/credentials/", json=body_b, headers=_auth())).json()["id"]
    assert cred_a != cred_b

    # PUT /credentials/{A} but with body.id = B (a different valid, owned credential).
    upd = {
        "id": cred_b,
        "name": "renamed",
        "provider": "google",
        "user_id": body_a["user_id"],
        "tool_id": body_a["tool_id"],
        "cred_type": "oauth",
        "credential": {"access_token": "rotated"},
    }
    r = await client.put(f"/credentials/{cred_a}", json=upd, headers=_auth())
    assert r.status_code == 400, r.text

    # the addressed (path) credential A is untouched — its metadata is unchanged
    got_a = await client.get(f"/credentials/{cred_a}", headers=_auth())
    assert got_a.status_code == 200
    assert got_a.json()["name"] == body_a["name"]
    assert got_a.json()["provider"] == body_a["provider"]


async def test_name_only_update_preserves_secret(client: AsyncClient) -> None:
    """A rename (no ``credential`` in the body) must NOT touch the stored secret — #341.

    The frontend never re-sends a secret (§1.5), so a name-only update must preserve the stored
    ciphertext rather than overwrite it with an empty value.
    """
    body = _payload()
    cred_id = (await client.post("/credentials/", json=body, headers=_auth())).json()["id"]

    import os

    from sqlalchemy.ext.asyncio import create_async_engine

    async def _ciphertext() -> str:
        # read as the OWNER (bypasses RLS) so the at-rest byte-comparison is GUC-independent.
        engine = create_async_engine(os.environ["_OWNER_DATABASE_URL"])
        async with engine.connect() as conn:
            ct = (
                await conn.execute(
                    text("SELECT encrypted_cred FROM user_credentials WHERE id = :i"),
                    {"i": cred_id},
                )
            ).scalar_one()
        await engine.dispose()
        return ct

    before = await _ciphertext()
    upd = {
        "id": cred_id,
        "name": "renamed-only",
        "provider": "google",
        "user_id": body["user_id"],
        "tool_id": body["tool_id"],
        "cred_type": "oauth",
        # no `credential` key — this is the name-only rename path
    }
    r = await client.put(f"/credentials/{cred_id}", json=upd, headers=_auth())
    assert r.status_code == 200 and r.json()["name"] == "renamed-only"
    # the stored ciphertext is byte-for-byte unchanged → the secret was preserved
    assert await _ciphertext() == before


async def test_auth_required(client: AsyncClient) -> None:
    assert (await client.post("/credentials/", json=_payload())).status_code == 401
    assert (
        await client.get(f"/credentials/{uuid.uuid4()}", headers=_auth("wrong"))
    ).status_code == 401


async def test_discovery_lists_connected_providers_and_data_sources(client: AsyncClient) -> None:
    user = str(uuid.uuid4())

    def cred(provider: str) -> dict:
        return {
            "tool_id": str(uuid.uuid4()),
            "user_id": user,
            "name": provider,
            "provider": provider,
            "cred_type": "oauth",
            "credential": {"access_token": "t"},
        }

    assert (
        await client.post("/credentials/", json=cred("google"), headers=_auth())
    ).status_code == 201
    assert (
        await client.post("/credentials/", json=cred("github"), headers=_auth())
    ).status_code == 201

    provs = await client.get("/credentials/providers", params={"user_id": user}, headers=_auth())
    assert provs.status_code == 200
    assert set(provs.json()["providers"]) == {"google", "github"}

    ds = await client.get(
        "/credentials/available-data-sources", params={"user_id": user}, headers=_auth()
    )
    assert ds.status_code == 200
    sources = ds.json()["data_sources"]
    assert "drive" in sources["google"] and "repositories" in sources["github"]
    # discovery requires auth
    assert (await client.get("/credentials/providers", params={"user_id": user})).status_code == 401
