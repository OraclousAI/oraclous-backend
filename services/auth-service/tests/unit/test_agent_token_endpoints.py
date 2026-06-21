"""Failing unit tests for the agent token + credential endpoints (R1-A2).

What these tests pin (R1-A2 acceptance criteria):

* ``POST /agent-token`` exchanges a raw agent credential for a JWT carrying
  ``principal_type=agent`` and the agent's ``organisation_id``. Bad credentials
  return 401 — never 5xx, never silent success.
* ``POST /internal/agent-credentials`` is gated by ``X-Internal-Key`` (legacy
  internal-service pattern) and returns the raw credential **exactly once**.
* ``DELETE /internal/agent-credentials/{agent_id}`` is gated and revokes every
  active credential for the agent (idempotent).
* ``GET /me`` resolves an agent token to a payload carrying
  ``principal_type=agent`` and ``organisation_id``. The post-revocation 401
  path is exercised (T2: revoked credential can never re-authenticate).

Behavioural reference (Lift): legacy
``auth-service/app/routes/auth_routes.py`` — ``/service-token``,
``/internal/service-account-keys`` (create + delete), ``/me``. Reshape: drop
``tenant_id``/``home_graph_id`` (legacy SA-only); add ``organisation_id``;
prefix ``osk_`` → ``oag_``; rename SA → agent.

These tests author against the ``oraclous_auth_service.app.create_app`` seam:
the implementer is expected to publish a small FastAPI app factory that
accepts an :class:`AgentRepository`-shaped object and an internal-key
verifier, so the routes are testable without a real database or Redis. The
factory wiring (production config loading, Postgres-backed store, Redis pipe)
lives in the implementation and is exercised at integration time, not here.

RED until ``oraclous_auth_service.app`` and the matching routes module exist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from oraclous_governance import jwt_audience, jwt_issuer

pytestmark = pytest.mark.unit

_ORG = "org-test-3333"
_USER = "user-creator-4444"
_INTERNAL_KEY = "internal-service-key-for-tests"


def _read_token_settings() -> tuple[str, str]:
    return (
        os.environ.setdefault("JWT_SECRET", "test-secret-for-ora-31-not-production"),
        os.environ.setdefault("JWT_ALGORITHM", "HS256"),
    )


# --- Test doubles ----------------------------------------------------------


@dataclass
class _FakeAgentRepository:
    """Test double standing in for the real :class:`AgentRepository`.

    Stores agents and their (raw → agent_id) mapping in memory and lets tests
    pre-seed agents whose ``raw`` is known. The shape mirrors the real
    repository (``create_agent``, ``validate_credential``, ``revoke_agent``).
    Persistence and bcrypt are not under test here — those live in
    test_agent_credential_lifecycle.
    """

    agents_by_id: dict[str, dict] = field(default_factory=dict)
    raw_to_agent_id: dict[str, str] = field(default_factory=dict)
    revoked_agent_ids: set[str] = field(default_factory=set)

    async def create_agent(
        self, *, organisation_id: str, created_by_user_id: str, principal_type: str = "agent"
    ) -> tuple[str, object]:
        import secrets
        import uuid

        agent_id = str(uuid.uuid4())
        raw = f"oag_{secrets.token_urlsafe(32)}"
        self.agents_by_id[agent_id] = {
            "id": agent_id,
            "organisation_id": organisation_id,
            "created_by_user_id": created_by_user_id,
            "principal_type": principal_type,
        }
        self.raw_to_agent_id[raw] = agent_id

        class _Stub:
            def __init__(self, agent_id: str) -> None:
                self.id = agent_id

        return raw, _Stub(agent_id)

    async def validate_credential(self, raw_credential: str) -> str | None:
        agent_id = self.raw_to_agent_id.get(raw_credential)
        if agent_id is None or agent_id in self.revoked_agent_ids:
            return None
        return agent_id

    async def revoke_agent(self, agent_id: str) -> int:
        if agent_id in self.revoked_agent_ids:
            return 0
        if agent_id not in self.agents_by_id:
            return 0
        self.revoked_agent_ids.add(agent_id)
        return 1

    async def organisation_id_for(self, agent_id: str) -> str | None:
        if agent_id in self.revoked_agent_ids:
            return None
        record = self.agents_by_id.get(agent_id)
        return record["organisation_id"] if record else None

    async def principal_type_for(self, agent_id: str) -> str | None:
        if agent_id in self.revoked_agent_ids:
            return None
        record = self.agents_by_id.get(agent_id)
        return record.get("principal_type", "agent") if record else None


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def fake_repo() -> _FakeAgentRepository:
    return _FakeAgentRepository()


@pytest.fixture
def client(fake_repo: _FakeAgentRepository) -> TestClient:
    _read_token_settings()
    os.environ.setdefault("INTERNAL_SERVICE_KEY", _INTERNAL_KEY)
    from oraclous_auth_service.app import create_app

    app = create_app(agent_repository=fake_repo, internal_service_key=_INTERNAL_KEY)
    return TestClient(app)


@pytest.fixture
def seeded_agent(fake_repo: _FakeAgentRepository) -> tuple[str, str]:
    """Seed one agent; return ``(raw_credential, agent_id)``."""
    import asyncio

    raw, agent = asyncio.get_event_loop().run_until_complete(
        fake_repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)
    )
    return raw, agent.id


# --- POST /agent-token -----------------------------------------------------


def test_agent_token_exchanges_credential_for_jwt(
    client: TestClient, seeded_agent: tuple[str, str]
) -> None:
    """A valid credential returns a JWT identifying ``agent`` + organisation_id."""
    secret, algorithm = _read_token_settings()
    raw, agent_id = seeded_agent

    response = client.post("/agent-token", json={"credential": raw})

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"  # noqa: S105 — JWT scheme name, not a secret
    assert body["principal_type"] == "agent"
    assert int(body["expires_in"]) > 0

    # iss/aud are stamped on every token (#356); pass them so the decode succeeds.
    claims = jwt.decode(
        body["access_token"],
        secret,
        algorithms=[algorithm],
        audience=jwt_audience(),
        issuer=jwt_issuer(),
    )
    assert claims["sub"] == agent_id
    assert claims["principal_type"] == "agent"
    assert claims["organisation_id"] == _ORG


def test_agent_token_rejects_invalid_credential(client: TestClient) -> None:
    """Unknown / well-formed-but-not-issued credential returns 401."""
    response = client.post("/agent-token", json={"credential": "oag_deadbeefdeadbeefdeadbeef"})
    assert response.status_code == 401


def test_agent_token_rejects_revoked_credential(
    client: TestClient, fake_repo: _FakeAgentRepository, seeded_agent: tuple[str, str]
) -> None:
    """A revoked credential can never be re-exchanged (T2 revocation race)."""
    raw, agent_id = seeded_agent
    import asyncio

    asyncio.get_event_loop().run_until_complete(fake_repo.revoke_agent(agent_id))

    response = client.post("/agent-token", json={"credential": raw})
    assert response.status_code == 401


def test_agent_token_rejects_wrong_prefix(client: TestClient) -> None:
    """A credential lacking the ``oag_`` scheme prefix is rejected with 401.

    The repository's prefix guard short-circuits before any store lookup; from
    the HTTP boundary, the visible behaviour is just 401 (no scheme leak in
    the error body).
    """
    response = client.post("/agent-token", json={"credential": "osk_NotAnAgentCredential"})
    assert response.status_code == 401


# --- POST /internal/agent-credentials -------------------------------------


def test_internal_create_returns_raw_credential_once(client: TestClient) -> None:
    """Internal create endpoint returns the raw credential exactly once."""
    response = client.post(
        "/internal/agent-credentials",
        headers={"X-Internal-Key": _INTERNAL_KEY},
        json={"organisation_id": _ORG, "created_by_user_id": _USER},
    )
    assert response.status_code == 201
    body = response.json()
    assert "agent_id" in body
    assert body["credential"].startswith("oag_")


def test_internal_create_requires_internal_key(client: TestClient) -> None:
    """Without the ``X-Internal-Key`` header the endpoint is 401/403, never 200."""
    response = client.post(
        "/internal/agent-credentials",
        json={"organisation_id": _ORG, "created_by_user_id": _USER},
    )
    assert response.status_code in (401, 403)


def test_internal_create_rejects_wrong_internal_key(client: TestClient) -> None:
    """A wrong ``X-Internal-Key`` is also rejected, not silently downgraded."""
    response = client.post(
        "/internal/agent-credentials",
        headers={"X-Internal-Key": "wrong-key"},
        json={"organisation_id": _ORG, "created_by_user_id": _USER},
    )
    assert response.status_code in (401, 403)


def test_internal_create_persists_organisation_id(
    client: TestClient, fake_repo: _FakeAgentRepository
) -> None:
    """The created agent is scoped to the requested ``organisation_id`` (ADR-006)."""
    response = client.post(
        "/internal/agent-credentials",
        headers={"X-Internal-Key": _INTERNAL_KEY},
        json={"organisation_id": _ORG, "created_by_user_id": _USER},
    )
    assert response.status_code == 201
    agent_id = response.json()["agent_id"]
    record = fake_repo.agents_by_id[agent_id]
    assert record["organisation_id"] == _ORG


# --- DELETE /internal/agent-credentials/{agent_id} ------------------------


def test_internal_revoke_revokes_active_credentials(
    client: TestClient, seeded_agent: tuple[str, str]
) -> None:
    """DELETE revokes; subsequent token exchange returns 401."""
    raw, agent_id = seeded_agent

    response = client.delete(
        f"/internal/agent-credentials/{agent_id}",
        headers={"X-Internal-Key": _INTERNAL_KEY},
    )
    assert response.status_code == 200
    assert response.json()["revoked_count"] == 1

    # Post-revocation exchange must now fail
    exchange = client.post("/agent-token", json={"credential": raw})
    assert exchange.status_code == 401


def test_internal_revoke_is_idempotent(client: TestClient, seeded_agent: tuple[str, str]) -> None:
    """Revoking twice returns ``revoked_count: 0`` the second time, not 5xx."""
    _, agent_id = seeded_agent

    first = client.delete(
        f"/internal/agent-credentials/{agent_id}",
        headers={"X-Internal-Key": _INTERNAL_KEY},
    )
    assert first.status_code == 200
    assert first.json()["revoked_count"] == 1

    second = client.delete(
        f"/internal/agent-credentials/{agent_id}",
        headers={"X-Internal-Key": _INTERNAL_KEY},
    )
    assert second.status_code == 200
    assert second.json()["revoked_count"] == 0


def test_internal_revoke_requires_internal_key(
    client: TestClient, seeded_agent: tuple[str, str]
) -> None:
    """No internal key → 401/403; the agent's credential remains valid."""
    raw, agent_id = seeded_agent

    response = client.delete(f"/internal/agent-credentials/{agent_id}")
    assert response.status_code in (401, 403)

    # The credential is still valid
    exchange = client.post("/agent-token", json={"credential": raw})
    assert exchange.status_code == 200


# --- GET /me ---------------------------------------------------------------


def test_me_resolves_agent_principal(client: TestClient, seeded_agent: tuple[str, str]) -> None:
    """An agent JWT resolves to a ``principal_type=agent`` payload with org id."""
    raw, agent_id = seeded_agent

    exchange = client.post("/agent-token", json={"credential": raw})
    assert exchange.status_code == 200
    token = exchange.json()["access_token"]

    me = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    me_body = me.json()
    assert me_body["id"] == agent_id
    assert me_body["principal_type"] == "agent"
    assert me_body["organisation_id"] == _ORG


def test_me_rejects_revoked_agent_token(
    client: TestClient,
    fake_repo: _FakeAgentRepository,
    seeded_agent: tuple[str, str],
) -> None:
    """Even an unexpired token must be rejected once its agent is revoked (T2)."""
    raw, agent_id = seeded_agent
    exchange = client.post("/agent-token", json={"credential": raw})
    token = exchange.json()["access_token"]

    import asyncio

    asyncio.get_event_loop().run_until_complete(fake_repo.revoke_agent(agent_id))

    me = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 401


def test_me_rejects_missing_token(client: TestClient) -> None:
    """``/me`` without a bearer token is 401."""
    response = client.get("/me")
    assert response.status_code == 401
