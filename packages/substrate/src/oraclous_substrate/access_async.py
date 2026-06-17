"""Async organisation-GUC binding seam (ADR-030 §2) — the RLS backstop for the
async SQLAlchemy services.

``packages/substrate/access.py`` already carries the *sync* psycopg seam
(``scoped_pg_connection`` / ``bind_organisation_guc``), but every R3.5 service is
async SQLAlchemy (asyncpg): they open ``create_async_engine`` sessions/connections
in ``repositories/`` and a sync psycopg context manager does not fit. ADR-030 §2
calls for an **async** equivalent — this module is it.

Two surfaces, both transaction-local (``set_config(..., is_local=true)`` — the bind
dies with the transaction, so a pooled connection never leaks one org's scope into
the next transaction):

* :func:`bind_org_guc_async` — bind the GUC on an *open* async connection/session.
  The org defaults to ``enforced_organisation_id()`` (fail-closed from the bound
  ``OrganisationContext``; never a request-body argument — T1-M1), or a caller may
  pass an explicit ``organisation_id`` resolved from authenticated context (the
  R3.5 services resolve the org as a value off the principal/internal header, then
  bind the governance context at the edge).

* :func:`install_org_guc_guard` — register a SQLAlchemy engine ``begin`` event that
  binds the GUC at the start of **every** transaction on that engine, sourcing the
  org from the bound ``OrganisationContext``. This is the load-bearing wiring: it
  fires for every repository idiom (``engine.begin()``, ``engine.connect()`` +
  ``conn.begin()``, ``session.begin()``, and a session's autobegin-on-first-execute)
  without threading a bind call through each call site. When **no** context is bound
  the GUC is left at the empty string, which the ``NULLIF(...,'')`` policy guard
  fails closed to zero rows — so an unscoped request never widens access. The
  privileged operator/backfill path (the owner/superuser DSN, which bypasses RLS)
  is unaffected: the empty GUC is irrelevant under a BYPASSRLS/owner-superuser role.

The contextvar set in async request code propagates into the synchronous greenlet
that runs the ``begin`` event (SQLAlchemy copies the context across
``greenlet_spawn``), so the event reads the request's bound org correctly.

The role precondition (NOSUPERUSER/NOBYPASSRLS) is asserted once at startup by
:func:`assert_non_bypassing_role` — the async mirror of ``scoped_pg_connection``'s
``rolsuper``/``rolbypassrls`` chokepoint (T1-M3): a service mis-deployed under a
bypassing role voids the backstop, so it fails closed loudly rather than silently.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from oraclous_governance.propagation import (
    MissingOrganisationContextError,
    current_organisation_context,
)
from sqlalchemy import event, text

from oraclous_substrate.access import enforced_organisation_id
from oraclous_substrate.schema.postgres import ORG_GUC

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession


class RlsBypassingRoleError(RuntimeError):
    """The runtime DB role bypasses RLS (rolsuper/rolbypassrls) — the backstop is void."""


async def bind_org_guc_async(
    target: AsyncSession | AsyncConnection,
    *,
    organisation_id: str | None = None,
) -> None:
    """Bind the RLS GUC (``app.current_organisation_id``) transaction-locally on an
    open async session/connection (ADR-030 §2).

    The org defaults to ``enforced_organisation_id()`` (fail-closed from the bound
    ``OrganisationContext``); pass ``organisation_id`` to bind an explicit
    authenticated-context org. The value travels as a bound parameter, never
    interpolated (injection-safe; T1). Must run inside an open transaction so the
    ``SET LOCAL`` has a transaction to be local to.
    """
    org = organisation_id if organisation_id is not None else enforced_organisation_id()
    await target.execute(text("SELECT set_config(:k, :v, true)"), {"k": ORG_GUC, "v": org})


def install_org_guc_guard(engine: AsyncEngine) -> None:
    """Register a ``begin`` event on ``engine`` that binds the org GUC at the start
    of every transaction from the bound ``OrganisationContext`` (ADR-030 §2).

    Fires for every repository idiom. When no context is bound the GUC is set to the
    empty string (the ``NULLIF`` policy guard then fails closed to zero rows) rather
    than raising — an unscoped read denies, never widens, and the privileged
    operator path (owner/superuser DSN that bypasses RLS) is unaffected. Idempotent:
    re-installing on the same engine is a no-op.
    """
    sync_engine = engine.sync_engine
    if event.contains(sync_engine, "begin", _on_begin):
        return
    event.listen(sync_engine, "begin", _on_begin)


def _on_begin(connection: Connection) -> None:
    """SQLAlchemy ``begin`` event: bind the GUC from context, or leave it empty
    (fail-closed) when no context is bound. Runs synchronously in the greenlet that
    drives asyncpg; ``exec_driver_sql`` issues the ``SET LOCAL`` on the same
    just-begun transaction the caller will use.

    The org value is interpolated as a literal rather than bound, because this raw
    DBAPI execute spans drivers (asyncpg uses ``$1`` paramstyle, psycopg ``%s``) and
    a parameter would be driver-specific here. That is injection-safe: the value is
    always a ``uuid.UUID`` from authenticated context (never request input), and it is
    re-parsed through :class:`uuid.UUID` below so a non-uuid can never reach the SQL
    (it fails closed to the empty GUC instead). T1-M1.
    """
    try:
        raw = str(current_organisation_context().organisation_id)
        org = str(uuid.UUID(raw))  # re-parse: only a canonical uuid literal reaches the SQL
    except (MissingOrganisationContextError, ValueError):
        org = ""  # NULLIF('') → policy fails closed to zero rows; never widen access
    connection.exec_driver_sql(f"SELECT set_config('{ORG_GUC}', '{org}', true)")


async def assert_non_bypassing_role(engine: AsyncEngine) -> None:
    """Fail closed at startup if the runtime role bypasses RLS (ADR-030 §3).

    The async mirror of ``scoped_pg_connection``'s NOSUPERUSER/NOBYPASSRLS chokepoint
    (T1-M3): a superuser or ``BYPASSRLS`` role silently voids the A1 RLS backstop, so
    a service mis-deployed under one must refuse to come up rather than run with an
    inert policy. Raises :class:`RlsBypassingRoleError` on a bypassing role.
    """
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).first()
    if row is None:
        raise RlsBypassingRoleError("current_user has no pg_roles entry")
    rolsuper, rolbypassrls = bool(row[0]), bool(row[1])
    if rolsuper or rolbypassrls:
        raise RlsBypassingRoleError(
            "runtime DB role bypasses RLS "
            f"(rolsuper={rolsuper}, rolbypassrls={rolbypassrls}) — ADR-030 §3 "
            "requires a NOSUPERUSER NOBYPASSRLS runtime role for the RLS backstop"
        )
