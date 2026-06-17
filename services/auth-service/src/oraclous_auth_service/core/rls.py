"""Postgres RLS wiring for the auth-service identity store (ADR-030 Slice 1, core connection layer).

The RLS seam is now canonical in the substrate (``oraclous_substrate.access_async``); this module is
a thin **re-export shim** so auth's local import surface (``from ...core.rls import
build_rls_engine, org_scope``) is unchanged while the implementation lives in exactly one place
across services (ADR-030 §2). It activates the row-level-security backstop for auth's
**always-org-bound** tables —
``agents`` and ``agent_credentials`` (both carry a NOT NULL ``organisation_id`` and are only ever
read/written within a single org's context).

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

This module re-exports the shared wiring both reach for:

* :func:`build_rls_engine` — construct an ``AsyncEngine`` with the substrate ``begin``-event guard
  installed, so every transaction binds ``app.current_organisation_id`` transaction-locally from the
  bound ``OrganisationContext`` (fail-closed to the empty GUC → zero rows when none bound, T1-M1).

* :func:`org_scope` — bind the org for an enclosed DB op so the engine guard sets the GUC. Auth
  carries the org as a ``str`` (a uuid string); the canonical ``org_scope`` accepts ``str |
  uuid.UUID`` and parses it to :class:`uuid.UUID` for the (uuid-typed) ``OrganisationContext`` —
  failing loud (``ValueError``, uncaught) on a non-uuid org, exactly as auth's variant did. RLS keys
  solely on the org; the principal id is a fixed marker here. Used by the isolation test to bind the
  org the way the service would.

* :func:`assert_runtime_role_isolates` — startup fail-closed: refuse to come up under a
  superuser / ``BYPASSRLS`` role, which would silently void the backstop (T1-M3). It is the
  substrate ``assert_non_bypassing_role`` re-exported under the service-local name the lifespan
  imports.

The privileged operator path — Alembic migrations + the owner-run grant bootstrap — connects on the
owner DSN (a superuser in the dev stack, which bypasses RLS); the empty GUC there is irrelevant.
"""

from __future__ import annotations

from oraclous_substrate.access_async import (
    RlsBypassingRoleError,
    assert_non_bypassing_role,
    build_rls_engine,
    org_scope,
)

# Service-local name the lifespan imports; the substrate assertion is the implementation.
assert_runtime_role_isolates = assert_non_bypassing_role

__all__ = [
    "RlsBypassingRoleError",
    "assert_runtime_role_isolates",
    "build_rls_engine",
    "org_scope",
]
