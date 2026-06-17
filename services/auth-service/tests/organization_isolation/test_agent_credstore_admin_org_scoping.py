"""Failing org-scoping tests for the Postgres-backed agent CredentialStore
(ORA-45, R1-A3).

Pins the two ADR-012 §1a invariants for the auth-service identity store:

* **(a) Pre-auth global resolve.** ``AgentRepository.validate_credential`` is a
  legitimate pre-authentication lookup and is *explicitly NOT* org-scoped — it
  does not accept (or implicitly require) an ``organisation_id`` parameter,
  and the SQL prefix index must guarantee that any active prefix resolves to
  **exactly one** principal (never a cross-org enumeration surface).
* **(b) Org-scoped admin paths.** The administrative paths (``list_for_organisation``,
  ``get_credential``, ``revoke_credential``) run under an authenticated
  internal/admin principal and must reject cross-organisation access: an admin
  in org A cannot list / get / revoke org B's agent credentials.

These two invariants are deliberately tested *together* in one file — they are
a pair, and a reviewer reading "admin paths are org-scoped" should immediately
see "but the pre-auth validate path is not, and here is why that is safe".

Behavioural distinction (ADR-012 §1a): the agent credential store is the
auth-service identity store — a distinct enforcement domain, NOT the
tenant-scoped knowledge substrate. ``validate_credential`` MUST NOT route
through ``oraclous_substrate.access`` (the prefix lookup precedes authentication
and there is no actor context yet to scope by). Tests live in
``services/auth-service/tests/...`` rather than ``tests/organization_isolation/``
so this suite can be run in isolation when the shared pytest session aborts
collection on an unmerged substrate import (the soft-coupling fallback in the
ORA-45 brief).

RED until ``backend-implementer`` adds the real
``oraclous_auth_service.repositories.postgres_credential_store.PostgresCredentialStore``
with the admin surface plus a SQL constraint pinning prefix uniqueness for
active rows.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import AsyncIterator

import pytest
from oraclous_auth_service.repositories.agent_repository import AgentRepository
from oraclous_auth_service.repositories.postgres_credential_store import (
    PostgresCredentialStore,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.organization_isolation,
    pytest.mark.security,
]


def _asyncpg_url(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
async def store(postgres_dsn: str) -> AsyncIterator[PostgresCredentialStore]:
    s = PostgresCredentialStore(_asyncpg_url(postgres_dsn))
    await s.create_tables()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def repo(store: PostgresCredentialStore) -> AgentRepository:
    return AgentRepository(store=store)


@pytest.fixture
def org_a() -> str:
    """A unique organisation id for the test's primary org per run.

    A canonical UUID string (as in production, ``str(uuid.uuid4())``): the org-bound store
    ops bind it via ``org_scope`` → the engine GUC guard, which re-parses it as a
    ``uuid.UUID``. Per-test uniqueness keeps the session-scoped Postgres container's
    accumulated rows from bleeding into ``list_for_organisation`` / ``get_credential``
    assertions that expect a known cardinality.
    """
    return str(uuid.uuid4())


@pytest.fixture
def org_b() -> str:
    """A second unique organisation id, distinct from ``org_a`` (canonical UUID, as in prod)."""
    return str(uuid.uuid4())


@pytest.fixture
def admin() -> str:
    """A unique admin/creating-user id per test."""
    return f"user-admin-{uuid.uuid4()}"


# --- (b) Admin paths are org-scoped within auth-service ---------------------


async def test_list_for_organisation_returns_only_that_orgs_credentials(
    repo: AgentRepository,
    store: PostgresCredentialStore,
    org_a: str,
    org_b: str,
    admin: str,
) -> None:
    """An admin in org A listing agent credentials must not see org B's."""
    _raw_a, agent_a = await repo.create_agent(organisation_id=org_a, created_by_user_id=admin)
    _raw_b, _agent_b = await repo.create_agent(organisation_id=org_b, created_by_user_id=admin)

    listed = await store.list_for_organisation(org_a)
    assert [c.agent_id for c in listed] == [agent_a.id]
    assert all(c.organisation_id == org_a for c in listed)


async def test_get_credential_within_organisation_returns_credential(
    repo: AgentRepository, store: PostgresCredentialStore, org_a: str, admin: str
) -> None:
    """Happy path: the credential's owning org can fetch it by id."""
    _raw, _agent = await repo.create_agent(organisation_id=org_a, created_by_user_id=admin)
    (created,) = await store.list_for_organisation(org_a)

    fetched = await store.get_credential(organisation_id=org_a, credential_id=created.id)
    assert fetched is not None
    assert fetched.id == created.id


async def test_cross_organisation_get_credential_is_denied(
    repo: AgentRepository,
    store: PostgresCredentialStore,
    org_a: str,
    org_b: str,
    admin: str,
) -> None:
    """ADR-012 §1a (b): an admin in org B cannot fetch org A's credential by id."""
    _raw, _agent = await repo.create_agent(organisation_id=org_a, created_by_user_id=admin)
    (created,) = await store.list_for_organisation(org_a)

    leaked = await store.get_credential(organisation_id=org_b, credential_id=created.id)
    assert leaked is None, "an agent credential must not be readable from another organisation"


async def test_cross_organisation_revoke_credential_is_denied(
    repo: AgentRepository,
    store: PostgresCredentialStore,
    org_a: str,
    org_b: str,
    admin: str,
) -> None:
    """ADR-012 §1a (b): a cross-org revoke is a no-op and the victim survives."""
    raw, agent = await repo.create_agent(organisation_id=org_a, created_by_user_id=admin)
    (created,) = await store.list_for_organisation(org_a)

    revoked = await store.revoke_credential(organisation_id=org_b, credential_id=created.id)
    assert revoked is False, "cross-org revoke must not affect the victim's credential"

    # The owning org's credential survives both the admin read and the pre-auth validate.
    survivor = await store.get_credential(organisation_id=org_a, credential_id=created.id)
    assert survivor is not None
    assert survivor.status == "active"
    assert await repo.validate_credential(raw) == agent.id


# --- (a) Pre-auth global resolve — explicitly NOT org-scoped ----------------


def test_validate_credential_takes_no_organisation_parameter() -> None:
    """ADR-012 §1a (a): ``validate_credential`` is a pre-auth GLOBAL resolve.

    Signature pin: it must not accept ``organisation_id`` (or any synonym) —
    routing the pre-auth path through an org filter would either require an
    actor context that does not yet exist or silently scope the lookup, both
    of which are wrong for an identity store.
    """
    sig = inspect.signature(AgentRepository.validate_credential)
    param_names = {name for name in sig.parameters if name != "self"}
    forbidden = {"organisation_id", "org", "org_id", "organisation"}
    assert param_names.isdisjoint(forbidden), (
        f"validate_credential must not accept an org parameter; found {param_names & forbidden}"
    )


async def test_validate_credential_resolves_without_any_org_context(
    repo: AgentRepository, org_a: str, admin: str
) -> None:
    """Functional pin: a credential validates from a bare prefix, no org needed.

    The auth-service identity store sits BEFORE the substrate's org-context is
    populated — there is no authenticated actor yet to derive an org from.
    Routing this path through ``oraclous_substrate.access`` would be both
    architecturally wrong (ADR-012 §1a) and operationally impossible.
    """
    raw, agent = await repo.create_agent(organisation_id=org_a, created_by_user_id=admin)
    # No org-context fixture, no org argument — validate just works.
    assert await repo.validate_credential(raw) == agent.id


async def test_active_prefix_is_globally_unique_at_persistence_layer(
    repo: AgentRepository, postgres_dsn: str, org_a: str, org_b: str, admin: str
) -> None:
    """ADR-012 §1a (a): the active-prefix index resolves to exactly one principal.

    Pinned at the schema layer so the invariant holds regardless of how the
    application code happens to be wired. Inserting a second *active* row
    sharing the prefix of an existing active row must be rejected by the
    database (e.g. UNIQUE INDEX ... WHERE status = 'active'). This is what
    keeps ``active_credentials_by_prefix`` from becoming a cross-org
    enumeration surface — a prefix maps to at most one active agent, ever.
    """
    import psycopg

    # Seed an active credential in org A through the public path so we have a
    # known-good prefix bound to an active row.
    raw_a, _agent_a = await repo.create_agent(organisation_id=org_a, created_by_user_id=admin)
    prefix = raw_a[:12]

    # Attempt to insert a second active row sharing the prefix, via raw SQL,
    # bypassing the application code. The database must reject the insert.
    with (
        pytest.raises(psycopg.errors.UniqueViolation),
        psycopg.connect(postgres_dsn, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "INSERT INTO agent_credentials "
            "(id, agent_id, organisation_id, credential_hash, credential_prefix, status) "
            "VALUES (%s, %s, %s, %s, %s, 'active')",
            (
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                org_b,
                "$2b$12$not.a.real.hash.collision.placeholder.value.string.padding.x",
                prefix,
            ),
        )
