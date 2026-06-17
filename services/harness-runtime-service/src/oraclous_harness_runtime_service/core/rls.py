"""Postgres RLS wiring for the harness-runtime-service (ADR-030 / #353, core connection layer).

The RLS seam is canonical in the substrate (``oraclous_substrate.access_async``); this module is a
thin **re-export shim** so the service's local import surface
(``from ...core.rls import build_rls_engine, org_scope``) is uniform with the other realized
services while the implementation lives in exactly one place (ADR-030 §2). It activates the
row-level-security backstop for the four org-scoped harness tables — ``harness_executions``,
``harness_checkpoints``, ``harness_assignments`` and ``harness_provenance`` — all STRICT org
isolation (the harness has no shared platform-catalogue case, so no read-widening):

* :func:`build_rls_engine` — the ONE way the service constructs a runtime ``AsyncEngine``. It
  installs the substrate ``begin``-event guard so **every** transaction on that engine binds
  ``app.current_organisation_id`` transaction-locally from the bound ``OrganisationContext`` (and
  fails closed to the empty GUC — zero rows — when no context is bound). The harness-specific
  caveat: the four tables are owned by FOUR independent repositories (``ExecutionRepository``,
  ``CheckpointRepository``, ``AssignmentRepository`` and ``PostgresProvenanceSink``) that each
  construct their own engine on the same DSN — every one of them builds its engine through here, so
  no engine that touches an RLS-enabled harness table is left unguarded (a single missed factory
  would leave that table's writes failing FORCE'd RLS under the runtime role).

* :func:`org_scope` — the repository-side chokepoint: each repository method binds the org it
  already received as an argument (resolved from authenticated context — the request principal on
  the user surface, the trusted caller's ``organisation_id`` on the X-Internal-Key surface, never a
  request body) for the duration of a DB op, so the engine guard reads it (mirroring the
  credential-broker, which wraps every op in ``org_scope``). The harness does NOT bind a governance
  context at the request edge, so threading ``org_scope`` through every repository op is what makes
  the bound GUC present before each query — without it the begin-guard binds the empty GUC and every
  write fails closed under the FORCE'd policy. RLS only needs the org; the principal id is a marker
  here (the GUC policy keys solely on ``organisation_id``). The service passes a ``uuid.UUID`` (its
  org columns are ``uuid``); the canonical ``org_scope`` accepts ``str | uuid.UUID`` and normalises
  through :class:`uuid.UUID`, round-tripping an existing UUID.

* :func:`assert_runtime_role_isolates` — startup fail-closed: refuse to come up under a superuser /
  ``BYPASSRLS`` role, which would silently void the backstop (T1-M3). It is the substrate
  ``assert_non_bypassing_role`` re-exported under the service-local name the lifespan imports.

The privileged operator path — the Alembic migrate + rls-role bootstrap one-shot — connects on the
**owner** DSN, which is a superuser in the dev stack and therefore bypasses RLS; the empty GUC the
guard binds there is irrelevant. The harness has no Celery/background worker (all DB access is
in-request through the four repositories constructed in the FastAPI lifespan), so the web-startup
role assertion is the only role check needed — there is no out-of-request worker engine to guard.
Only the long-running runtime service switches its DSN to the NOSUPERUSER ``oraclous_app`` role
(ADR-030 §3).
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
