"""Postgres RLS wiring for the auth-service identity store (ADR-030 Slice 1, core connection layer).

The substrate carries the generic seam (``oraclous_substrate.access_async``); this module is the
service-local wiring that activates the row-level-security backstop for auth's **always-org-bound**
tables — ``agents`` and ``agent_credentials`` (both carry a NOT NULL ``organisation_id`` and are
only ever read/written within a single org's context).

Auth has a nuance Slice 0 (credential-broker) did not: it holds BOTH always-org-bound tables AND
login/identity/cross-org tables that are accessed *without* a bound org (``users``/``organisations``
identity, ``org_members`` enumerated across a user's orgs at login, ``auth_audit_log`` with a
nullable org for pre-org events, ``org_invitations``/``oauth_accounts``/``refresh_tokens`` reached
in pre-org / token-lookup flows). Only the former pair gets RLS — see ADR-030 + the
``rls_coverage.yaml`` exclusions for why the latter must NOT (RLS would fail-close login).

Two engines, deliberately split (ADR-030 §3 + ADR-012 §1a):

* The **identity engine** (``core/database.make_engine`` → the user/org/oauth/member/invitation/
  refresh/audit sessionmaker) connects as the NOSUPERUSER ``oraclous_app`` role and carries the
  GUC guard (:func:`build_rls_engine`). Its own tables are NOT RLS-enabled (all excluded), so the
  guard binds the empty GUC there harmlessly; running these no-bound-org flows under the runtime
  role is what proves login/refresh don't fail-close. It asserts its role at startup.

* The **credential store** (:class:`PostgresCredentialStore`, which touches ``agents`` /
  ``agent_credentials``) stays on the OWNER DSN. It is the ADR-012 §1a org-context PRODUCER — its
  validate-by-prefix / org-resolve are pre-auth GLOBAL lookups that MUST resolve across orgs, so it
  must NOT be org-scoped/RLS-enforced on its connection. RLS on those two tables is therefore the
  *backstop* (defense-in-depth), proven under ``oraclous_app`` by the data-layer isolation test
  (app-WHERE removed), not enforced on the store's owner connection.

This module is the shared wiring both reach for:

* :func:`build_rls_engine` — construct an ``AsyncEngine`` with the substrate ``begin``-event guard
  installed, so every transaction binds ``app.current_organisation_id`` transaction-locally from the
  bound ``OrganisationContext`` (fail-closed to the empty GUC → zero rows when none bound, T1-M1).

* :func:`org_scope` — bind the org for an enclosed DB op so the engine guard sets the GUC. Auth
  carries the org as a ``str`` (a uuid string), so this parses it to :class:`uuid.UUID` for the
  (uuid-typed) ``OrganisationContext``. RLS keys solely on the org; the principal id is a fixed
  marker here. Used by the isolation test to bind the org the way the service would.

* :func:`assert_runtime_role_isolates` — startup fail-closed: refuse to come up under a
  superuser / ``BYPASSRLS`` role, which would silently void the backstop (T1-M3).

The privileged operator path — Alembic migrations + the owner-run grant bootstrap — connects on the
owner DSN (a superuser in the dev stack, which bypasses RLS); the empty GUC there is irrelevant.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from typing import Any

from oraclous_governance import (
    OrganisationContext,
    PrincipalType,
    use_organisation_context,
)
from oraclous_substrate.access_async import (
    RlsBypassingRoleError,
    assert_non_bypassing_role,
    install_org_guc_guard,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

__all__ = [
    "RlsBypassingRoleError",
    "assert_runtime_role_isolates",
    "build_rls_engine",
    "org_scope",
]

# A fixed marker principal for the GUC-binding context. RLS keys only on organisation_id, so the
# principal value is never consulted by the policy; this keeps org_scope usable from a store method
# that holds the org but not a full principal.
_RLS_PRINCIPAL = uuid.UUID("00000000-0000-0000-0000-0000000005d5")


def build_rls_engine(db_url: str, **engine_kwargs: Any) -> AsyncEngine:
    """Create an ``AsyncEngine`` with the RLS org-GUC guard installed (ADR-030 §2).

    The single constructor the credential store uses so no engine touching ``agents`` /
    ``agent_credentials`` can be created without the per-transaction org binding. Engine kwargs
    (``echo``, ``pool_pre_ping``) pass through unchanged.
    """
    engine = create_async_engine(db_url, **engine_kwargs)
    install_org_guc_guard(engine)
    return engine


@contextlib.contextmanager
def org_scope(organisation_id: str) -> Iterator[None]:
    """Bind the org for the enclosed DB operation so the engine guard sets the GUC.

    Auth carries the org as a ``str`` (a canonical uuid string in production); this parses it to a
    :class:`uuid.UUID` for the (uuid-typed) ``OrganisationContext``. Idempotent w.r.t. nesting
    (``use_organisation_context`` restores the prior binding on exit). The org is sourced from
    authenticated context by the caller — never a request body — preserving the fail-closed T1-M1
    invariant.
    """
    context = OrganisationContext(
        organisation_id=uuid.UUID(str(organisation_id)),
        principal_id=_RLS_PRINCIPAL,
        principal_type=PrincipalType.SERVICE_ACCOUNT,
    )
    with use_organisation_context(context):
        yield


async def assert_runtime_role_isolates(engine: AsyncEngine) -> None:
    """Fail closed at startup unless the runtime role is NOSUPERUSER/NOBYPASSRLS.

    The async mirror of ``scoped_pg_connection``'s role chokepoint (ADR-030 §3 / T1-M3): a bypassing
    role makes the RLS policy inert, so the service must refuse to start rather than run with a void
    backstop. Delegates to the substrate assertion.
    """
    await assert_non_bypassing_role(engine)
