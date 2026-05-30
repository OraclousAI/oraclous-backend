"""Failing integration tests for the real Postgres-backed agent CredentialStore
(ORA-45, R1-A3).

Pins, against a real Postgres (ORA-12 / 0d harness), the contract the ORA-30
unit suite asserts against the in-memory double:

* ``persist`` writes the agent + credential to SQL;
* ``active_credentials_by_prefix`` does the prefix lookup and applies the
  ``WHERE status = 'active'`` filter at the SQL layer — revoked rows are not
  returned;
* ``revoke_agent_credentials`` performs the ``UPDATE status = 'revoked'`` and
  reports the affected rowcount;
* credential **expiry** is evaluated by the ``AgentRepository`` (not the
  store), so an expired-but-still-active row is rejected by validate but is
  still returned by the bare port lookup;
* the full ``AgentRepository`` round-trip (create → validate → revoke → expiry)
  works end-to-end against real SQL.

The in-memory double remains for the ORA-30 unit suite; nothing here mocks the
database. Behavioural reference: legacy
``auth-service/app/repositories/service_account_repository.py`` (lift-tag
**Reshape**) — same ``WHERE status='active'`` + prefix-index pattern, refit
behind the ORA-30 port and onto the agent principal per ADR-006.

RED until ``backend-implementer`` adds the real
``oraclous_auth_service.repositories.postgres_credential_store`` module
implementing the ORA-30 ``CredentialStore`` port against SQLAlchemy + Postgres.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from oraclous_auth_service.repositories.agent_repository import AgentRepository
from oraclous_auth_service.repositories.postgres_credential_store import (
    PostgresCredentialStore,
)

pytestmark = pytest.mark.integration

_ORG = "org-aaaa"
_OTHER_ORG = "org-bbbb"
_USER = "user-1234"


def _asyncpg_url(dsn: str) -> str:
    """Adapt the libpq DSN from the harness to the async driver SQLAlchemy uses.

    Mirrors the convention established by the credential-broker integration
    suite (``tests/organization_isolation/test_credential_broker_org_scoping``).
    If the implementer picks a different async driver, this helper is the only
    line to update.
    """
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
async def store(postgres_dsn: str) -> AsyncIterator[PostgresCredentialStore]:
    """A fresh PostgresCredentialStore against the session-scoped container.

    Tables are created idempotently per test; tests use fresh UUIDs/orgs so they
    do not collide on shared rows.
    """
    s = PostgresCredentialStore(_asyncpg_url(postgres_dsn))
    await s.create_tables()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def repo(store: PostgresCredentialStore) -> AgentRepository:
    """The ORA-30 ``AgentRepository`` wired against the real Postgres store."""
    return AgentRepository(store=store)


# --- ORA-30 port contract against real SQL ----------------------------------


async def test_create_persists_agent_and_credential_in_sql(
    repo: AgentRepository, store: PostgresCredentialStore
) -> None:
    """``persist`` writes both the agent and its credential row to Postgres.

    Round-trip via the store's own admin read; we are pinning that ``persist``
    is not silently a no-op (a regression that would pass the in-memory unit
    suite trivially).
    """
    raw, agent = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)
    assert raw.startswith("oag_")

    persisted = await store.list_for_organisation(_ORG)
    assert [c.agent_id for c in persisted] == [agent.id]
    assert persisted[0].organisation_id == _ORG
    assert persisted[0].status == "active"


async def test_round_trip_validate_returns_owning_agent_id(
    repo: AgentRepository,
) -> None:
    """A freshly created credential validates to its owning agent's id."""
    raw, agent = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)
    assert await repo.validate_credential(raw) == agent.id


async def test_active_credentials_by_prefix_excludes_revoked_at_sql_layer(
    repo: AgentRepository, store: PostgresCredentialStore
) -> None:
    """``WHERE status='active'`` lives in the SQL — revoked rows are filtered.

    Pins the contract the ORA-30 unit suite asserts on the double: the status
    filter belongs in the store, not the repository.
    """
    raw, agent = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)
    prefix = raw[:12]

    before = await store.active_credentials_by_prefix(prefix)
    assert [c.agent_id for c in before] == [agent.id]

    revoked = await store.revoke_agent_credentials(agent.id)
    assert revoked == 1

    after = await store.active_credentials_by_prefix(prefix)
    assert after == [], "revoked credentials must not be returned by the active-prefix lookup"


async def test_revoke_agent_credentials_persists_update_and_breaks_validate(
    repo: AgentRepository,
) -> None:
    """``revoke_agent_credentials`` runs the SQL ``UPDATE`` and validate then fails."""
    raw, agent = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)
    assert await repo.validate_credential(raw) == agent.id

    revoked = await repo.revoke_agent(agent.id)
    assert revoked == 1

    assert await repo.validate_credential(raw) is None


async def test_revoke_is_idempotent_at_sql(
    repo: AgentRepository,
) -> None:
    """A second revoke for the same agent reports zero affected rows."""
    _raw, agent = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)
    assert await repo.revoke_agent(agent.id) == 1
    assert await repo.revoke_agent(agent.id) == 0


async def test_expired_credential_is_rejected_by_repo_not_store(
    repo: AgentRepository, store: PostgresCredentialStore
) -> None:
    """Repo-side expiry check: the row is still ``status='active'`` in SQL.

    Pins the split-of-concerns the ORA-30 port asserts — the SQL filter is
    ``status='active'`` only; the ``expires_at`` clock comparison happens in
    ``AgentRepository.validate_credential``. A regression that moved expiry
    into SQL would change the rowcount returned by the bare port method.
    """
    raw, _agent = await repo.create_agent(
        organisation_id=_ORG,
        created_by_user_id=_USER,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    # validate rejects it (repo-side clock check)
    assert await repo.validate_credential(raw) is None

    # but the bare port lookup still finds it (status is still 'active')
    prefix = raw[:12]
    found = await store.active_credentials_by_prefix(prefix)
    assert len(found) == 1
    assert found[0].status == "active"
    assert found[0].expires_at is not None and found[0].expires_at < datetime.now(UTC)


async def test_unexpired_credential_validates_against_real_sql(
    repo: AgentRepository,
) -> None:
    """A credential with a future expiry validates end-to-end via SQL."""
    raw, agent = await repo.create_agent(
        organisation_id=_OTHER_ORG,
        created_by_user_id=_USER,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert await repo.validate_credential(raw) == agent.id


async def test_tampered_credential_fails_bcrypt_after_real_sql_prefix_hit(
    repo: AgentRepository,
) -> None:
    """A tampered credential sharing a real prefix is rejected by bcrypt verify.

    Re-asserts (against real SQL) the unit suite's bcrypt-verify branch: the
    prefix lookup hits, then the bcrypt check refuses the wrong body.
    """
    raw, _agent = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)
    tampered = raw[:-1] + ("A" if raw[-1] != "A" else "B")

    assert await repo.validate_credential(tampered) is None
