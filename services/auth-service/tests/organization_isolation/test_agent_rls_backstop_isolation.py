"""Data-layer proof that the Postgres RLS backstop is a LIVE control on auth's always-org-bound
tables (``agents`` / ``agent_credentials``) — exercised on the ACTUAL runtime path: the real
:class:`PostgresCredentialStore` org-bound engine, connected as the NOSUPERUSER/NOBYPASSRLS
``oraclous_app`` role (ADR-030 Slice 1, with the credential-store access-pattern split).

This is deliberately NOT a synthetic probe. Earlier the only engine touching these tables was the
credential store on the OWNER (superuser) DSN, which bypasses RLS — so the policy never bit on a
runtime connection and the backstop was latent. The store now SPLITS its DB access by pattern: the
org-bound ops (``persist`` / ``list_for_organisation`` / ``get_credential`` / ``revoke_credential``)
run on a GUC-guarded engine as ``oraclous_app`` (RLS bites), while the pre-auth cross-org resolves
(``active_credentials_by_prefix`` / ``organisation_id_for`` / ``principal_type_for`` /
``revoke_agent_credentials``) stay on the owner engine (must resolve across orgs). These tests
build the store with the SAME two-DSN wiring ``main.py`` uses and prove the policy bites on the
store's own org-bound engine.

Two complementary proofs:

* **Backstop (app-WHERE removed).** Using the store's own org-bound engine (``store._org_engine``)
  — the exact connection the org-bound store methods use at runtime — a bare ``SELECT`` with NO
  ``organisation_id`` predicate returns only the bound org's rows, a cross-org write is denied
  (SQLSTATE 42501), and an unbound scope returns zero rows (fail-closed, T1-M1). RLS alone scopes —
  a bug that dropped the app-layer ``WHERE`` no longer leaks cross-org rows.

* **End-to-end via the public store methods.** The org-scoped admin surface, driven through its
  real methods under ``oraclous_app``, isolates org A from org B (list / get / revoke), and the
  pre-auth cross-org resolve still works on the owner engine. This proves the split is
  behaviour-neutral except for the now-live enforcement.

Run as the ``oraclous_app`` role (RLS only bites a non-superuser). The org GUC is bound exactly as
the store binds it (``org_scope`` → the engine ``begin`` guard), never a hand-written WHERE. Orgs
are canonical UUIDs (as in production), so the policy's ``organisation_id::uuid`` column cast (auth
stores the org as ``String`` — Slice 1's nuance vs Slice 0's uuid column) is exercised.

Threats: T1-M1, T1-M3. ADR-006; ADR-012 §1a/§2; ADR-030.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import pytest
from oraclous_auth_service.models import Base
from oraclous_auth_service.repositories.agent_repository import AgentRepository
from oraclous_auth_service.repositories.postgres_credential_store import PostgresCredentialStore
from oraclous_substrate.schema import postgres as pg_schema
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.organization_isolation,
    pytest.mark.security,
    pytest.mark.isolation,
]

ORG_A = str(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
ORG_B = str(uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"))

APP_ROLE = "oraclous_app"
APP_PASSWORD = "app"  # noqa: S105 — ephemeral test-container role, not a real secret

# auth's always-org-bound tables RLS is enabled on (0007_enable_rls). organisation_id is a String
# column, so enable_rls_on must cast it for the policy comparison (org_column_is_uuid=False).
_RLS_TABLES = ("agents", "agent_credentials")

# Direct SQL against agent_credentials with NO organisation_id predicate — RLS is the only scoping.
_SELECT_ALL_IDS = "SELECT agent_id FROM agent_credentials"
_COUNT_ALL = "SELECT count(*) FROM agent_credentials"
_INSERT_CRED = (
    "INSERT INTO agent_credentials "
    "(id, agent_id, organisation_id, credential_hash, credential_prefix, status) "
    "VALUES (:id, :agent, :org, 'h', :prefix, 'active')"
)
_INSERT_AGENT = (
    "INSERT INTO agents (id, organisation_id, created_by_user_id) VALUES (:id, :org, :user)"
)


def _provision_app_role(superuser_libpq_dsn: str) -> None:
    import psycopg

    with psycopg.connect(superuser_libpq_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN "
            f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}' "
            "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE; END IF; END $$;"
        )
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
        cur.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
        )


def _to_app_userinfo(async_dsn: str) -> str:
    parts = urlsplit(async_dsn)
    netloc = f"{APP_ROLE}:{APP_PASSWORD}@{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@pytest.fixture
async def store(postgres_dsn: str) -> AsyncIterator[PostgresCredentialStore]:
    """A real :class:`PostgresCredentialStore` wired exactly as ``main.py`` wires it: the owner DSN
    for cross-org resolves + the ``oraclous_app`` DSN for the org-bound engine (RLS bites). Schema,
    RLS, and the role are provisioned by the SUPERUSER owner first, as the migrate one-shot does."""
    owner_async = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    app_async = _to_app_userinfo(owner_async)

    # schema via SQLAlchemy (asyncpg, owner); RLS DDL via a sync psycopg connection — enable_rls_on
    # speaks the sync DB-API cursor protocol (the same path the Alembic migration uses). The String
    # org column requires org_column_is_uuid=False (auth's Slice-1 nuance).
    owner_engine = create_async_engine(owner_async)
    async with owner_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await owner_engine.dispose()
    import psycopg

    with psycopg.connect(postgres_dsn, autocommit=True) as raw:
        for table in _RLS_TABLES:
            pg_schema.enable_rls_on(raw, table, org_column_is_uuid=False)
    _provision_app_role(postgres_dsn)

    s = PostgresCredentialStore(owner_async, org_bound_db_url=app_async)
    try:
        yield s
    finally:
        await s.close()


def _ctx(org: str):  # noqa: ANN202
    from oraclous_governance import OrganisationContext, PrincipalType

    return OrganisationContext(
        organisation_id=uuid.UUID(org), principal_id=uuid.uuid4(), principal_type=PrincipalType.USER
    )


async def test_rls_alone_isolates_reads_on_the_store_org_bound_engine(
    store: PostgresCredentialStore,
) -> None:
    """The store's OWN org-bound engine (the runtime connection, under ``oraclous_app``) scopes by
    RLS alone — a bare SELECT with NO ``organisation_id`` WHERE returns only the bound org's rows,
    and an unbound scope returns zero (fail-closed). This is the backstop with the app-WHERE
    removed, proven on the live path rather than a hand-built engine."""
    from oraclous_governance import use_organisation_context

    repo = AgentRepository(store=store)
    # WRITE org A's agent+credential through the real store path (persist → org-bound engine, WITH
    # CHECK admits the matching-org write under org A's GUC).
    await repo.create_agent(organisation_id=ORG_A, created_by_user_id=str(uuid.uuid4()))

    # store._org_engine is the exact connection the org-bound store methods use at runtime.
    org_engine = store._org_engine  # noqa: SLF001

    # READ under org A's GUC on that engine: the credential is visible, with NO organisation_id
    # WHERE — RLS alone scopes it.
    with use_organisation_context(_ctx(ORG_A)):
        async with org_engine.begin() as conn:
            a_rows = (await conn.execute(text(_SELECT_ALL_IDS))).all()
    assert len(a_rows) == 1

    # READ under org B's GUC: org A's credential is INVISIBLE (RLS USING filters it).
    with use_organisation_context(_ctx(ORG_B)):
        async with org_engine.begin() as conn:
            b_rows = (await conn.execute(text(_SELECT_ALL_IDS))).all()
    assert b_rows == []

    # FAIL-CLOSED: with NO org context bound, the guard binds the empty GUC → zero rows (an absent
    # scope denies, never defaults — T1-M1). org A's real row stays hidden.
    async with org_engine.begin() as conn:
        assert (await conn.execute(text(_COUNT_ALL))).scalar_one() == 0


async def test_cross_org_write_is_denied_on_the_store_org_bound_engine(
    store: PostgresCredentialStore,
) -> None:
    """With org B bound, inserting a row stamped for org A (smuggled — not the bound org) violates
    the RLS WITH CHECK → SQLSTATE 42501. Proven on the store's own org-bound engine; both
    always-org-bound tables carry the same WITH CHECK."""
    from oraclous_governance import use_organisation_context

    org_engine = store._org_engine  # noqa: SLF001

    with pytest.raises(ProgrammingError) as exc_info:
        with use_organisation_context(_ctx(ORG_B)):
            async with org_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT_CRED),
                    {
                        "id": str(uuid.uuid4()),
                        "agent": str(uuid.uuid4()),
                        "org": ORG_A,  # smuggled — not the bound org
                        "prefix": "oag_smuggle",
                    },
                )
    assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"

    with pytest.raises(ProgrammingError) as exc_info2:
        with use_organisation_context(_ctx(ORG_B)):
            async with org_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT_AGENT),
                    {"id": str(uuid.uuid4()), "org": ORG_A, "user": str(uuid.uuid4())},
                )
    assert getattr(exc_info2.value.orig, "sqlstate", None) == "42501"


async def test_admin_surface_isolates_through_the_real_store_methods(
    store: PostgresCredentialStore,
) -> None:
    """End-to-end on the live path: the org-scoped admin methods, driven through their real
    signatures under ``oraclous_app`` (RLS biting beneath the app-layer WHERE), isolate org A from
    org B for list / get / revoke; and the pre-auth cross-org resolve still works on the owner
    engine — behaviour-neutral except the now-live enforcement."""
    repo = AgentRepository(store=store)
    raw_a, agent_a = await repo.create_agent(
        organisation_id=ORG_A, created_by_user_id=str(uuid.uuid4())
    )
    await repo.create_agent(organisation_id=ORG_B, created_by_user_id=str(uuid.uuid4()))

    # list is org-scoped (org A sees only its own agent).
    listed_a = await store.list_for_organisation(ORG_A)
    assert [c.agent_id for c in listed_a] == [agent_a.id]
    assert all(c.organisation_id == ORG_A for c in listed_a)

    (cred_a,) = listed_a
    # get within the owning org succeeds; cross-org get returns None (RLS + app-WHERE).
    assert (await store.get_credential(organisation_id=ORG_A, credential_id=cred_a.id)) is not None
    assert (await store.get_credential(organisation_id=ORG_B, credential_id=cred_a.id)) is None

    # cross-org revoke is a no-op; the victim survives and still validates via the cross-org path.
    assert (await store.revoke_credential(organisation_id=ORG_B, credential_id=cred_a.id)) is False
    survivor = await store.get_credential(organisation_id=ORG_A, credential_id=cred_a.id)
    assert survivor is not None and survivor.status == "active"
    # pre-auth cross-org resolve (owner engine) still works — no org context required.
    assert await repo.validate_credential(raw_a) == agent_a.id


async def test_org_bound_engine_role_is_non_bypassing(store: PostgresCredentialStore) -> None:
    """The role the store's org-bound engine connects as must be NOSUPERUSER/NOBYPASSRLS, else the
    policy is inert (T1-M3) — the precondition the isolation above depends on, and what
    ``assert_runtime_role_isolates`` enforces at startup for the identity engine."""
    from oraclous_substrate.access_async import assert_non_bypassing_role

    # passes silently for oraclous_app; would raise RlsBypassingRoleError for a superuser.
    await assert_non_bypassing_role(store._org_engine)  # noqa: SLF001

    async with store._org_engine.connect() as conn:  # noqa: SLF001
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).first()
    assert row is not None
    assert row[0] is False and row[1] is False  # NOSUPERUSER, NOBYPASSRLS
