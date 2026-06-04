"""Postgres-backed agent credential store for the auth-service identity
domain (ORA-45 / R1-A3).

Implements the ORA-30 ``CredentialStore`` Protocol against SQLAlchemy 2.0
async + asyncpg, plus the org-scoped administrative surface (ADR-012 §1a (b)).
Lift-tag **Reshape** of ``auth-service/app/repositories/service_account_repository.py``
in the legacy worktree: same ``WHERE status='active'`` filter + prefix-index
lookup + ``UPDATE`` revoke, refit behind the ORA-30 port and onto the agent
principal.

Architecture (ADR-012 §1a): the auth-service identity store is a distinct
enforcement domain from the tenant-scoped knowledge substrate.

* The port method :meth:`active_credentials_by_prefix` is a deliberate
  pre-authentication global resolve and is **not** routed through
  ``oraclous_substrate.access`` — no organisation context exists yet at the
  point of credential validation. Invariant (a) — that an active prefix
  resolves to **exactly one** principal — is pinned at the schema layer by
  the partial UNIQUE INDEX on ``credential_prefix WHERE status = 'active'``
  declared in :mod:`oraclous_auth_service.models.agent_model`.
* The admin methods (:meth:`list_for_organisation` / :meth:`get_credential` /
  :meth:`revoke_credential`) are org-scoped — every read and write filters
  ``organisation_id`` from the calling admin's authenticated context.
  Invariant (b).

The repository (:class:`oraclous_auth_service.repositories.agent_repository.AgentRepository`)
owns bcrypt hashing/verification and the ``expires_at`` clock check; this
store owns persistence and the SQL-level filters.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oraclous_auth_service.models.agent_model import Agent, AgentCredential
from oraclous_auth_service.models.base import Base


class PostgresCredentialStore:
    """Postgres implementation of the ORA-30 CredentialStore port + admin surface.

    Lifecycle mirrors the credential-broker repository:
    :meth:`create_tables` is an idempotent dev/test convenience over
    ``Base.metadata.create_all``; :meth:`close` disposes the engine.
    """

    def __init__(self, db_url: str) -> None:
        self._engine = create_async_engine(db_url, echo=False)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def create_tables(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    # --- ORA-30 CredentialStore port (pre-auth — NOT org-scoped) -------------

    async def persist(self, agent: Agent, credential: AgentCredential) -> None:
        """Insert the agent and its credential in a single transaction."""
        async with self._session_factory() as session:
            session.add(agent)
            session.add(credential)
            await session.commit()

    async def active_credentials_by_prefix(self, prefix: str) -> list[AgentCredential]:
        """``WHERE credential_prefix = :prefix AND status = 'active'``.

        Deliberately not org-scoped (ADR-012 §1a (a)); the partial UNIQUE INDEX
        on the underlying column guarantees the result is at most one row,
        regardless of how many credentials have ever existed in any org.
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

        Returns the number of rows affected (the ORA-30 unit suite pins this
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
            return result.rowcount or 0

    async def organisation_id_for(self, agent_id: str) -> str | None:
        """Return the agent's organisation_id iff it has an *active* credential, else ``None``.

        Pre-auth global resolve (like ``active_credentials_by_prefix``): used by ``/agent-token``
        (after credential validation) and ``/me`` (revocation re-check, T2) — an agent whose
        credentials are all revoked resolves to ``None`` and can never re-authenticate.
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

        Used by ``/agent-token`` to mint the right token type for the credential's principal.
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

    # --- ADR-012 §1a (b): org-scoped administrative surface -------------------

    async def list_for_organisation(self, organisation_id: str) -> list[AgentCredential]:
        """All credentials owned by the given org (any status). Org-scoped."""
        async with self._session_factory() as session:
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

        Returns ``None`` when the credential id does not exist *or* exists in
        a different organisation — the two cases are deliberately
        indistinguishable to a cross-org caller (no enumeration oracle).
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentCredential).where(
                    AgentCredential.id == credential_id,
                    AgentCredential.organisation_id == organisation_id,
                )
            )
            return result.scalar_one_or_none()

    async def revoke_credential(self, *, organisation_id: str, credential_id: str) -> bool:
        """Revoke a single credential, scoped to its owning org.

        Returns ``True`` iff a row was actually transitioned from ``active`` to
        ``revoked``; ``False`` on cross-org attempts, unknown ids, and already-
        revoked credentials alike.
        """
        async with self._session_factory() as session:
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
            return (result.rowcount or 0) > 0
