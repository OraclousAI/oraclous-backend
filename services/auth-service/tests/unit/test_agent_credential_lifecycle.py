"""Failing unit tests for the agent principal model + credential lifecycle (R1-A1).

Behavioural reference: legacy ``auth-service/app/models/service_account_model.py``
(``AgentServiceAccountKey``) and ``app/repositories/service_account_repository.py``
(``ServiceAccountRepository``), **reshaped** from the service-account-key pattern to
a first-class ``agent`` principal. Lift-tag: Lift.

What these tests pin (R1-A1 acceptance criteria):
- a credential is generated **once** with an ``oag_`` prefix, stored only as a
  bcrypt hash, prefix-indexed, and never retrievable after creation;
- ``AgentRepository.validate_credential()`` does a prefix-guarded prefix lookup
  followed by a bcrypt verify, and rejects expired and revoked credentials;
- ``organisation_id`` is stored on the agent at creation (mirrors the legacy SA
  ``tenant_id``), per ADR-006;
- credentials whose prefix is not ``oag_`` are rejected by the guard *before* any
  store lookup (Structured Threat Catalogue T2 — new principal, no escalation path).

Out of scope here (do not test): token issuance + endpoints (A2) and
delegation (Epic B).

These tests describe behaviour, not implementation. The repository's persistence
is injected as a port; ``_InMemoryCredentialStore`` is a test double standing in
for the real Postgres-backed store (deferred to an integration story). Real
bcrypt hashing/verification and credential generation run inside the repository
under test.

RED until backend-implementer creates ``oraclous_auth_service.models.agent_model``
and ``oraclous_auth_service.repositories.agent_repository``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from oraclous_auth_service.models.agent_model import Agent, AgentCredential
from oraclous_auth_service.repositories.agent_repository import AgentRepository

pytestmark = pytest.mark.unit

_ORG = "org-aaaa"
_USER = "user-1234"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _InMemoryCredentialStore:
    """Test double for the agent-credential persistence seam.

    Stands in for the real Postgres-backed store. Mirrors the legacy SA query
    semantics: a prefix lookup returns only *active* credentials (the
    status filter lives in the store / SQL ``WHERE status = 'active'``), while
    credential **expiry** is evaluated by the repository. ``prefix_lookups``
    records every lookup so a test can assert the repository's prefix guard
    short-circuits before touching the store.
    """

    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}
        self.credentials: list[AgentCredential] = []
        self.prefix_lookups: list[str] = []

    async def persist(self, agent: Agent, credential: AgentCredential) -> None:
        self.agents[agent.id] = agent
        self.credentials.append(credential)

    async def active_credentials_by_prefix(self, prefix: str) -> list[AgentCredential]:
        self.prefix_lookups.append(prefix)
        return [
            c for c in self.credentials if c.credential_prefix == prefix and c.status == "active"
        ]

    async def revoke_agent_credentials(self, agent_id: str) -> int:
        count = 0
        for c in self.credentials:
            if c.agent_id == agent_id and c.status == "active":
                c.status = "revoked"
                count += 1
        return count


@pytest.fixture
def store() -> _InMemoryCredentialStore:
    return _InMemoryCredentialStore()


@pytest.fixture
def repo(store: _InMemoryCredentialStore) -> AgentRepository:
    return AgentRepository(store=store)


# --- creation --------------------------------------------------------------


async def test_create_agent_returns_oag_prefixed_credential_once(
    repo: AgentRepository, store: _InMemoryCredentialStore
) -> None:
    """create_agent returns the raw credential exactly once, ``oag_``-prefixed.

    The raw secret is the *return value only*; nothing equal to it is persisted.
    """
    raw, agent = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)

    assert isinstance(agent, Agent)
    assert raw.startswith("oag_")

    # The raw secret never appears in any persisted field.
    (persisted,) = store.credentials
    assert isinstance(persisted, AgentCredential)
    assert persisted.credential_hash != raw
    assert raw not in vars(persisted).values()


async def test_create_stores_organisation_id_on_agent(
    repo: AgentRepository,
) -> None:
    """ADR-006: the agent carries its organisation_id from creation."""
    _raw, agent = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)
    assert agent.organisation_id == _ORG


async def test_credential_is_stored_hashed_and_prefix_indexed(
    repo: AgentRepository, store: _InMemoryCredentialStore
) -> None:
    """Persisted credential holds a bcrypt hash and an ``oag_`` prefix index."""
    raw, agent = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)
    (persisted,) = store.credentials

    assert persisted.agent_id == agent.id
    assert persisted.status == "active"
    # Prefix index: a proper prefix of the raw key, carrying the scheme tag.
    assert persisted.credential_prefix.startswith("oag_")
    assert raw.startswith(persisted.credential_prefix)
    # Hash is genuinely a hash, not the secret, and not trivially reversible.
    assert persisted.credential_hash != raw
    assert raw not in persisted.credential_hash


async def test_distinct_agents_get_distinct_credentials(
    repo: AgentRepository,
) -> None:
    """Two creations yield distinct agents and distinct raw credentials."""
    raw_a, agent_a = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)
    raw_b, agent_b = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)
    assert agent_a.id != agent_b.id
    assert raw_a != raw_b


# --- validation ------------------------------------------------------------


async def test_validate_accepts_freshly_created_credential(
    repo: AgentRepository,
) -> None:
    """A just-issued credential validates to its owning agent's id."""
    raw, agent = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)
    assert await repo.validate_credential(raw) == agent.id


async def test_validate_rejects_wrong_prefix_without_touching_store(
    repo: AgentRepository, store: _InMemoryCredentialStore
) -> None:
    """A credential lacking the ``oag_`` prefix is rejected by the guard early.

    Mirrors the legacy ``if not api_key.startswith("osk_"): return None`` guard:
    no store lookup is performed for a malformed prefix.
    """
    assert await repo.validate_credential("sk_not_an_agent_credential") is None
    assert store.prefix_lookups == []


async def test_validate_rejects_unknown_credential(
    repo: AgentRepository,
) -> None:
    """A well-formed but never-issued credential validates to None."""
    assert await repo.validate_credential("oag_deadbeefdeadbeefdeadbeef") is None


async def test_validate_rejects_tampered_credential_via_bcrypt(
    repo: AgentRepository, store: _InMemoryCredentialStore
) -> None:
    """A credential sharing a real one's prefix but a wrong body fails bcrypt verify.

    The store *is* consulted (prefix matches), so rejection comes from the
    bcrypt verify step, not the prefix guard.
    """
    raw, _agent = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)
    tampered = raw[:-1] + ("A" if raw[-1] != "A" else "B")

    assert await repo.validate_credential(tampered) is None
    assert store.prefix_lookups  # the prefix lookup did run


async def test_validate_rejects_expired_credential(
    repo: AgentRepository,
) -> None:
    """An expired (but still active-status) credential is rejected by the repo."""
    raw, _agent = await repo.create_agent(
        organisation_id=_ORG,
        created_by_user_id=_USER,
        expires_at=_utc_now() - timedelta(minutes=1),
    )
    assert await repo.validate_credential(raw) is None


async def test_validate_accepts_unexpired_credential(
    repo: AgentRepository,
) -> None:
    """A credential with a future expiry still validates."""
    raw, agent = await repo.create_agent(
        organisation_id=_ORG,
        created_by_user_id=_USER,
        expires_at=_utc_now() + timedelta(hours=1),
    )
    assert await repo.validate_credential(raw) == agent.id


# --- revocation ------------------------------------------------------------


async def test_revoke_agent_invalidates_its_credential(
    repo: AgentRepository, store: _InMemoryCredentialStore
) -> None:
    """Revoking an agent revokes its active credential and validation then fails."""
    raw, agent = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)
    assert await repo.validate_credential(raw) == agent.id

    revoked = await repo.revoke_agent(agent.id)
    assert revoked == 1

    (persisted,) = store.credentials
    assert persisted.status == "revoked"
    assert await repo.validate_credential(raw) is None


async def test_revoke_agent_is_idempotent(
    repo: AgentRepository,
) -> None:
    """Re-revoking an agent with no active credentials reports zero revoked."""
    _raw, agent = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)
    assert await repo.revoke_agent(agent.id) == 1
    assert await repo.revoke_agent(agent.id) == 0
