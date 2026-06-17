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

import contextlib
import uuid
from typing import TYPE_CHECKING, Any

from oraclous_governance import (
    OrganisationContext,
    PrincipalType,
    use_organisation_context,
)
from oraclous_governance.propagation import (
    MissingOrganisationContextError,
    current_organisation_context,
)
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from oraclous_substrate.access import enforced_organisation_id
from oraclous_substrate.schema.postgres import ORG_GUC

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

# A fixed marker principal for the GUC-binding context on the per-tx scope. RLS keys
# only on ``organisation_id``, so the principal value is never consulted by the policy
# — this keeps :func:`org_scope` usable from a repository/store method that holds the
# org but not a full principal (the trusted-caller / worker / store-admin DB paths).
_RLS_PRINCIPAL = uuid.UUID("00000000-0000-0000-0000-0000000005d5")


class RlsBypassingRoleError(RuntimeError):
    """The runtime DB role bypasses RLS (rolsuper/rolbypassrls) — the backstop is void."""


def build_rls_engine(dsn: str, *, echo: bool = False, **engine_kwargs: Any) -> AsyncEngine:
    """Create an ``AsyncEngine`` with the RLS org-GUC guard installed (ADR-030 §2).

    The ONE constructor every realized service uses for a runtime engine, so no engine
    that touches an RLS-enabled table can be created without the per-transaction org
    binding. It opens ``create_async_engine`` then installs the
    :func:`install_org_guc_guard` ``begin`` event, exactly as each service's local
    ``core/rls.build_rls_engine`` did before this was hoisted into the substrate.

    ``echo`` is surfaced as a named parameter (the common per-repository idiom is
    ``build_rls_engine(url, echo=False)``); any further SQLAlchemy engine kwargs
    (``pool_pre_ping``, ``future``, …) pass through ``**engine_kwargs`` unchanged, so
    every existing call site keeps its exact engine configuration.
    """
    engine = create_async_engine(dsn, echo=echo, **engine_kwargs)
    install_org_guc_guard(engine)
    return engine


@contextlib.contextmanager
def org_scope(organisation_id: str | uuid.UUID) -> Iterator[None]:
    """Bind the org for the enclosed DB operation so the engine guard sets the GUC (ADR-030 §2).

    The repository-side chokepoint: a repo/store method binds the org it already received
    from authenticated context (the request principal, the trusted caller's
    ``organisation_id`` on the X-Internal-Key surface, or the task org in a worker) for
    the duration of the DB operation, so the :func:`install_org_guc_guard` ``begin`` event
    reads it and binds ``app.current_organisation_id`` transaction-locally. The org is
    sourced from authenticated context by the caller — never a request body — preserving
    the fail-closed T1-M1 invariant. Idempotent w.r.t. nesting:
    ``use_organisation_context`` restores the prior binding on exit.

    The org is accepted as ``str | uuid.UUID`` and normalised through
    :class:`uuid.UUID` for the (uuid-typed) :class:`OrganisationContext`. This unifies the
    two former per-service variants without changing either caller's behaviour:

    * The credential-broker passed a ``uuid.UUID`` (its org columns are ``uuid``);
      ``uuid.UUID(str(u))`` round-trips an existing UUID to the same value, so the bound
      context is byte-for-byte what the broker bound before.
    * The auth-service passes a ``str`` (its org columns are ``text`` holding a uuid
      string) and its variant did ``uuid.UUID(str(org))`` **without** catching
      ``ValueError`` — a non-uuid org fails loud (safe: it never silently widens to an
      unscoped read). This canonical form keeps that exact fail-loud behaviour: a
      malformed org raises ``ValueError`` here, before any DB op.

    The per-row policy-predicate cast (the *column* side: ``text`` vs ``uuid`` org column)
    is orthogonal to this binding and stays configurable at the schema layer via
    ``enable_rls_on(..., org_column_is_uuid=...)``; the GUC value bound here is always a
    canonical uuid literal regardless.
    """
    context = OrganisationContext(
        organisation_id=uuid.UUID(str(organisation_id)),
        principal_id=_RLS_PRINCIPAL,
        principal_type=PrincipalType.SERVICE_ACCOUNT,
    )
    with use_organisation_context(context):
        yield


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


def provision_app_role_ddl(
    *,
    role: str,
    password: str,
    tables: tuple[str, ...] | list[str],
    grant_all_tables: bool = False,
) -> list[str]:
    """The idempotent DDL that provisions the NOSUPERUSER/NOBYPASSRLS runtime role +
    GRANTs (ADR-030 §3) — the reusable bootstrap-grant statements each service's
    bootstrap CLI runs. Pure (returns the statements; runs nothing), so it is trivially
    testable and the side-effecting connect/execute stays in the caller.

    Emits, in order:

    1. An idempotent role create — ``DO $$ … IF NOT EXISTS (pg_roles) … CREATE ROLE
       <role> LOGIN PASSWORD '<password>' NOSUPERUSER NOBYPASSRLS NOCREATEDB
       NOCREATEROLE; … $$;`` — so the FORCE'd RLS policy bites the runtime role.
    2. ``GRANT USAGE ON SCHEMA public TO <role>``.
    3. The DML grant. Two shapes, selected by ``grant_all_tables`` to match the two
       existing services exactly:

       * ``grant_all_tables=False`` (credential-broker): one
         ``GRANT SELECT, INSERT, UPDATE, DELETE ON public."<t>" TO <role>`` per table in
         ``tables`` (grant only the RLS-enabled tables — the broker's runtime touches
         only those).
       * ``grant_all_tables=True`` (auth-service): a single
         ``GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO <role>``
         — the identity engine runs as this role and reads/writes every identity table
         (users/organisations/org_members/oauth/refresh_tokens/invitations/audit), so a
         grant limited to the RLS pair would fail those flows closed at first query.
    4. ``ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE
       ON TABLES TO <role>`` — covers any FUTURE table a later migration creates without
       re-listing it.

    Only trusted module constants (a per-service role/password/table registry) are
    interpolated — never request input. ``tables`` is still required (and used for the
    per-table grants when ``grant_all_tables`` is False); a caller using the broad grant
    passes its RLS table list for documentation/parity even though the grant is on
    ``ALL TABLES``.
    """
    create_role = (
        f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN "
        f"CREATE ROLE {role} LOGIN PASSWORD '{password}' "
        "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE; END IF; END $$;"
    )
    grant_usage = f"GRANT USAGE ON SCHEMA public TO {role}"
    if grant_all_tables:
        dml_grants: list[str] = [
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"
        ]
    else:
        dml_grants = [
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON public."{t}" TO {role}' for t in tables
        ]
    alter_default = (
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
    )
    return [create_role, grant_usage, *dml_grants, alter_default]


def provision_app_role(
    conn: Any,
    *,
    role: str,
    password: str,
    tables: tuple[str, ...] | list[str],
    grant_all_tables: bool = False,
) -> None:
    """Run :func:`provision_app_role_ddl` against an open psycopg connection (ADR-030 §3).

    The reusable bootstrap-grant helper each service's ``core/bootstrap_rls_role`` CLI
    delegates to with its own role/password/table list. Idempotent + re-runnable: the
    role create is guarded by ``IF NOT EXISTS`` and GRANT is additive, so a fresh role or
    re-deploy converges. ``conn`` must be an autocommit psycopg connection (matching the
    services' ``psycopg.connect(dsn, autocommit=True)``); transaction control stays the
    caller's. See ``provision_app_role_ddl`` for the ``grant_all_tables`` semantics.
    """
    with conn.cursor() as cur:
        for stmt in provision_app_role_ddl(
            role=role, password=password, tables=tables, grant_all_tables=grant_all_tables
        ):
            cur.execute(stmt)  # only trusted module constants are executed
