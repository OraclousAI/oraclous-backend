"""Postgres-backed agent credential store for the auth-service identity
domain (R1-A3; RLS made live for the org-bound path by ADR-030 Slice 1).

Implements the ``CredentialStore`` Protocol against SQLAlchemy 2.0
async + asyncpg, plus the org-scoped administrative surface (ADR-012 §1a (b)).
Lift-tag **Reshape** of ``auth-service/app/repositories/service_account_repository.py``
in the legacy worktree: same ``WHERE status='active'`` filter + prefix-index
lookup + ``UPDATE`` revoke, refit behind the port and onto the agent
principal.

Architecture (ADR-012 §1a + ADR-030 §2/§3): the auth-service identity store is a
distinct enforcement domain from the tenant-scoped knowledge substrate, and — unlike a
single-pattern repository — it holds BOTH org-bound CRUD AND pre-auth cross-org
resolves. So its DB access is **split by access pattern across two engines**, which is
what turns the RLS policy on ``agents`` / ``agent_credentials`` from latent into a LIVE
control on the runtime path:

* **Org-bound engine** (``_org_engine`` — built via :func:`build_rls_engine`, the
  NOSUPERUSER/NOBYPASSRLS ``oraclous_app`` runtime role). Carries the substrate
  org-GUC guard, so every transaction binds ``app.current_organisation_id`` from the
  org wrapped in :func:`org_scope`. The org-bound ops route here — an org context
  exists (or is carried on the row) for each:

  - :meth:`persist` (the org is on the agent/credential being created — internal
    agent-create, ``organisation_id`` from the trusted caller),
  - :meth:`list_for_organisation` / :meth:`get_credential` / :meth:`revoke_credential`
    (the admin surface — every read/write filters the calling admin's org, ADR-012
    §1a (b)).

  RLS **bites** here: a cross-org read returns zero rows and a cross-org write is denied
  (SQLSTATE 42501) by the policy, on top of the app-layer ``WHERE`` — the backstop is
  live on the actual runtime connection, not just a synthetic probe.

* **Owner engine** (``_engine`` — the privileged OWNER DSN, a superuser in the dev
  stack, which bypasses RLS by necessity). The pre-auth cross-org resolves route here —
  they precede any org context and MUST resolve across orgs (the ADR-012 §1a (a)
  org-context PRODUCER, like user-login):

  - :meth:`active_credentials_by_prefix` (validate a raw credential by its prefix index;
    invariant (a) — an active prefix resolves to exactly one principal — is pinned at
    the schema layer by the partial UNIQUE INDEX on ``credential_prefix WHERE
    status='active'``),
  - :meth:`organisation_id_for` / :meth:`principal_type_for` (resolve an agent's org /
    principal type from its agent_id after credential validation — ``/agent-token`` +
    the ``/me`` revocation re-check, T2),
  - :meth:`revoke_agent_credentials` (the agent-lifecycle revoke keyed by ``agent_id``
    with NO org in scope — ``DELETE /internal/agent-credentials/{agent_id}``; routing it
    through the org-bound engine with no bound org would fail-close to zero rows and
    break revoke, so it stays cross-org like the resolves).

The repository (:class:`oraclous_auth_service.repositories.agent_repository.AgentRepository`)
owns bcrypt hashing/verification and the ``expires_at`` clock check; this store owns
persistence and the SQL-level filters.

Backward compatibility: ``org_bound_db_url`` defaults to the owner ``db_url`` when not
supplied (a single-DSN construction — used by the unit/integration fixtures that run on
the owner/superuser DSN). Under that fallback the org-bound ops still bind the GUC via
``org_scope`` but the superuser bypasses RLS, so behaviour is identical to before; the
LIVE RLS proof runs the org-bound engine as ``oraclous_app`` (the deployed runtime role
and the isolation test). ``main.py`` wires ``org_bound_db_url`` to the ``oraclous_app``
identity DSN in production.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_auth_service.core.rls import build_rls_engine, org_scope
from oraclous_auth_service.models.agent_model import Agent, AgentCredential
from oraclous_auth_service.models.base import Base


class PostgresCredentialStore:
    """Postgres implementation of the CredentialStore port + admin surface.

    Two engines, split by access pattern (ADR-030 §2/§3): the org-bound CRUD runs on the
    GUC-guarded ``oraclous_app`` engine (RLS bites), the pre-auth cross-org resolves run
    on the owner engine (RLS bypassed by necessity). ``create_tables`` is an idempotent
    dev/test convenience over ``Base.metadata.create_all`` (run on the owner engine — it
    is DDL); :meth:`close` disposes both engines.
    """

    def __init__(self, db_url: str, *, org_bound_db_url: str | None = None) -> None:
        # Owner (privileged) engine — pre-auth cross-org resolves bypass RLS here by necessity.
        self._engine = create_async_engine(db_url, echo=False)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        # Org-bound engine — carries the substrate org-GUC guard, so org-bound ops wrapped in
        # ``org_scope`` bind app.current_organisation_id transaction-locally and RLS bites. Defaults
        # to the owner DSN for single-DSN (owner/superuser) constructions; the deployed runtime +
        # the isolation test pass the NOSUPERUSER oraclous_app DSN so the policy is a LIVE control.
        self._org_engine = build_rls_engine(org_bound_db_url or db_url, echo=False)
        self._org_session_factory = async_sessionmaker(self._org_engine, expire_on_commit=False)

    async def create_tables(self) -> None:
        # DDL runs on the owner engine (table creation is an owner privilege; org-scoping is
        # irrelevant to CREATE TABLE). The org-bound engine reuses the same schema.
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()
        await self._org_engine.dispose()

    # --- CredentialStore port: persist is org-bound (org-bound engine — RLS bites) -----

    async def persist(self, agent: Agent, credential: AgentCredential) -> None:
        """Insert the agent and its credential in a single transaction.

        Org-bound (ADR-030 §2): the org is carried on the rows being created (the internal
        agent-create supplies ``organisation_id`` from the trusted caller). Routed through the
        org-bound engine under ``org_scope`` so the RLS WITH CHECK admits the matching-org write
        (and would deny a mismatched one) — the backstop is live on the create path.
        """
        with org_scope(agent.organisation_id):
            async with self._org_session_factory() as session:
                session.add(agent)
                session.add(credential)
                await session.commit()

    # --- pre-auth cross-org resolves (owner engine — bypass RLS by necessity, like user-login) ---

    async def active_credentials_by_prefix(self, prefix: str) -> list[AgentCredential]:
        """``WHERE credential_prefix = :prefix AND status = 'active'``.

        Pre-auth cross-org resolve (ADR-012 §1a (a)) — runs on the owner engine because no
        organisation context exists at credential-validation time and the lookup must resolve
        across orgs. The partial UNIQUE INDEX on the underlying column guarantees the result is at
        most one row, regardless of how many credentials have ever existed in any org.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentCredential).where(
                    AgentCredential.credential_prefix == prefix,
                    AgentCredential.status == "active",
                )
            )
            return list(result.scalars().all())

    async def revoke_agent_credentials(self, agent_id: str) -> int:
        """``UPDATE ... SET status='revoked' WHERE agent_id = :id AND status='active'``.

        Keyed by ``agent_id`` with NO org in scope (the internal lifecycle revoke —
        ``DELETE /internal/agent-credentials/{agent_id}``), so it runs on the owner engine like the
        resolves: routing it through the org-bound engine with no bound org would fail-close to zero
        rows and break revoke. Returns the number of rows affected (the unit suite pins this
        as the revoke-count contract).
        """
        async with self._session_factory() as session:
            result = await session.execute(
                update(AgentCredential)
                .where(
                    AgentCredential.agent_id == agent_id,
                    AgentCredential.status == "active",
                )
                .values(status="revoked", revoked_at=datetime.now(UTC))
            )
            await session.commit()
            return cast("CursorResult[object]", result).rowcount or 0

    async def organisation_id_for(self, agent_id: str) -> str | None:
        """Return the agent's organisation_id iff it has an *active* credential, else ``None``.

        Pre-auth cross-org resolve (owner engine, like ``active_credentials_by_prefix``): used by
        ``/agent-token`` (after credential validation) and ``/me`` (revocation re-check, T2) — an
        agent whose credentials are all revoked resolves to ``None`` and can never re-authenticate.
        Keyed by ``agent_id`` with no org in scope, so it MUST resolve cross-org.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentCredential.organisation_id)
                .where(
                    AgentCredential.agent_id == agent_id,
                    AgentCredential.status == "active",
                )
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def principal_type_for(self, agent_id: str) -> str | None:
        """The principal_type (agent|service_account) of an active credential, else ``None``.

        Pre-auth cross-org resolve (owner engine): used by ``/agent-token`` to mint the right token
        type for the credential's principal. Keyed by ``agent_id`` with no org in scope.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentCredential.principal_type)
                .where(
                    AgentCredential.agent_id == agent_id,
                    AgentCredential.status == "active",
                )
                .limit(1)
            )
            return result.scalar_one_or_none()

    # --- ADR-012 §1a (b): org-scoped administrative surface (org-bound engine — RLS bites) ----

    async def list_for_organisation(self, organisation_id: str) -> list[AgentCredential]:
        """All credentials owned by the given org (any status). Org-scoped.

        Routed through the org-bound engine under ``org_scope``: the app-layer ``WHERE
        organisation_id`` is the primary filter, and the RLS policy is the live backstop (a dropped
        ``WHERE`` no longer leaks another org's rows). The org is the calling admin's, never a body.
        """
        with org_scope(organisation_id):
            async with self._org_session_factory() as session:
                result = await session.execute(
                    select(AgentCredential)
                    .where(AgentCredential.organisation_id == organisation_id)
                    .order_by(AgentCredential.created_at, AgentCredential.id)
                )
                return list(result.scalars().all())

    async def get_credential(
        self, *, organisation_id: str, credential_id: str
    ) -> AgentCredential | None:
        """Fetch a credential by id, scoped to its owning org.

        Org-bound engine under ``org_scope`` (RLS backstop live). Returns ``None`` when the
        credential id does not exist *or* exists in a different organisation — the two cases are
        deliberately indistinguishable to a cross-org caller (no enumeration oracle); RLS makes the
        cross-org row invisible even if the app-layer ``WHERE`` were dropped.
        """
        with org_scope(organisation_id):
            async with self._org_session_factory() as session:
                result = await session.execute(
                    select(AgentCredential).where(
                        AgentCredential.id == credential_id,
                        AgentCredential.organisation_id == organisation_id,
                    )
                )
                return result.scalar_one_or_none()

    async def revoke_credential(self, *, organisation_id: str, credential_id: str) -> bool:
        """Revoke a single credential, scoped to its owning org.

        Org-bound engine under ``org_scope`` (RLS backstop live). Returns ``True`` iff a row was
        actually transitioned from ``active`` to ``revoked``; ``False`` on cross-org attempts,
        unknown ids, and already-revoked credentials alike — and a cross-org row is unreachable to
        the UPDATE under RLS regardless of the app-layer ``WHERE``.
        """
        with org_scope(organisation_id):
            async with self._org_session_factory() as session:
                result = await session.execute(
                    update(AgentCredential)
                    .where(
                        AgentCredential.id == credential_id,
                        AgentCredential.organisation_id == organisation_id,
                        AgentCredential.status == "active",
                    )
                    .values(status="revoked", revoked_at=datetime.now(UTC))
                )
                await session.commit()
                return (cast("CursorResult[object]", result).rowcount or 0) > 0
