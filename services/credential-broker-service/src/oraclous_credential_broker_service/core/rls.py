"""Postgres RLS wiring for the credential-broker (ADR-030, core connection layer).

The RLS seam is now canonical in the substrate (``oraclous_substrate.access_async``);
this module is a thin **re-export shim** so the broker's local import surface
(``from ...core.rls import build_rls_engine, org_scope``) is unchanged while the
implementation lives in exactly one place across services (ADR-030 §2). It activates the
row-level-security backstop for the broker's four org-scoped tables (``user_credentials``,
``webhook_secrets``, ``delegated_tokens``, ``org_data_keys``):

* :func:`build_rls_engine` — the ONE way the service constructs a runtime ``AsyncEngine``.
  It installs the substrate ``begin``-event guard so **every** transaction on that engine
  binds ``app.current_organisation_id`` transaction-locally from the bound
  ``OrganisationContext`` (and fails closed to the empty GUC — zero rows — when no context
  is bound). Every repository builds its engine through here.

* :func:`org_scope` — the repository-side chokepoint: a repo method binds the org it
  already received from authenticated context (the request principal on the user surface,
  the trusted caller's ``organisation_id`` on the X-Internal-Key surface, or the task org
  in a worker) for the duration of the DB operation, so the engine guard reads it. RLS
  only needs the org; the principal id is a marker here (the GUC policy keys solely on
  ``organisation_id``). The broker passes a ``uuid.UUID`` (its org columns are ``uuid``);
  the canonical ``org_scope`` accepts ``str | uuid.UUID`` and normalises through
  :class:`uuid.UUID`, which round-trips an existing UUID to the same value.

* :func:`assert_runtime_role_isolates` — startup fail-closed: refuse to come up under a
  superuser / ``BYPASSRLS`` role, which would silently void the backstop (T1-M3). It is the
  substrate ``assert_non_bypassing_role`` re-exported under the service-local name the
  lifespan imports.

The privileged operator paths — Alembic migrations and the envelope backfill sweep (which
reads every org's ciphertext) — connect on the **owner** DSN, which is a superuser in the
dev stack and therefore bypasses RLS; the empty GUC the guard binds there is irrelevant.
Only the long-running runtime service switches its DSN to the NOSUPERUSER ``oraclous_app``
role (ADR-030 §3).
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
