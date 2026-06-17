"""Postgres RLS wiring for the execution-engine-service (ADR-030 / #353, core layer).

The RLS seam is canonical in the substrate (``oraclous_substrate.access_async``); this module is a
thin **re-export shim** so the service's local import surface (``from ...core.rls import
build_rls_engine, org_scope, assert_runtime_role_isolates``) is unchanged while the implementation
lives in exactly one place across services (ADR-030 §2). It activates the row-level-security
backstop for the engine's four org-scoped Postgres tables (``engine_jobs``, ``engine_schedules``,
``engine_roundtables``, ``engine_provenance``).

The execution-engine has a nuance the single-engine services (KGS, credential-broker) did not: its
request/driver path is org-bound (every repo read/write filters ``organisation_id`` and the Celery
task execution binds ``use_organisation_context``), **but three cross-org MAINTENANCE sweeps run
out-of-request with NO bound org** — the reaper (``JobRepository.list_stale_running`` +
``RoundtableRepository.list_stale_running``) and Celery Beat
(``ScheduleRepository.list_enabled_cron`` + ``set_last_fired``). Under FORCE'd RLS those would fail
closed to zero rows. So the DB access is
carved into TWO engines, mirroring the auth-service split (ADR-030 §3 + ADR-012 §1a):

* The **org-bound engine** — ``build_rls_engine(oraclous_app DSN)`` with the org-GUC guard
  installed. Used by ALL request/driver/org-bound repo methods AND the org-bound Celery task
  execution
  (``_run_async`` / ``_drive_roundtable_async`` bind ``use_organisation_context`` before any query).
  RLS BITES here: the guard binds ``app.current_organisation_id`` transaction-locally from the bound
  ``OrganisationContext`` per transaction (fail-closed to the empty GUC — zero rows — when none is
  bound, T1-M1). ``engine_provenance`` rides along on this engine (clean — every write carries the
  row's org).

* The **maintenance engine** — the OWNER DSN (a superuser in the dev stack, which BYPASSES RLS).
  Used ONLY by the cross-org sweeps' READS (``list_stale_running`` / ``list_enabled_cron``), which
  MUST keep reading across orgs. The per-row settle/transition AFTER a sweep goes back through the
  org-bound engine with the row's own org bound (``org_scope(row.organisation_id)``), so the write
  is RLS-scoped and a cross-org write is denied (SQLSTATE 42501) — no row crosses a tenant boundary.

This module re-exports the shared wiring both reach for:

* :func:`build_rls_engine` — construct an ``AsyncEngine`` with the substrate ``begin``-event guard
  installed (the org-bound engine factory).

* :func:`install_org_guc_guard` — register the ``begin``-event guard on an ``AsyncEngine`` directly
  (the engine's repositories build their own engines, so they install the guard via this rather than
  a single ``build_rls_engine`` call site — equivalent for each factory).

* :func:`org_scope` — bind the org for an enclosed DB op so the engine guard sets the GUC. The
  cross-org sweeps wrap each per-row settle in ``org_scope(row.organisation_id)`` so the org-bound
  write binds the row's org (the row's org comes from the maintenance read, never request input —
  T1-M1).

* :func:`assert_runtime_role_isolates` — startup fail-closed: refuse to come up under a superuser /
  ``BYPASSRLS`` role, which would silently void the backstop (T1-M3). It is the substrate
  ``assert_non_bypassing_role`` re-exported under the service-local name lifespan + worker import.

The privileged operator path — the Alembic migrations + the owner-run grant bootstrap, AND the
maintenance/reaper/beat read engine — connects on the OWNER DSN (a superuser in the dev stack, which
bypasses RLS); the empty GUC there is irrelevant under an owner/superuser role.
"""

from __future__ import annotations

from oraclous_substrate.access_async import (
    RlsBypassingRoleError,
    assert_non_bypassing_role,
    build_rls_engine,
    install_org_guc_guard,
    org_scope,
)

# Service-local name the lifespan + worker import; the substrate assertion is the implementation.
assert_runtime_role_isolates = assert_non_bypassing_role

__all__ = [
    "RlsBypassingRoleError",
    "assert_runtime_role_isolates",
    "build_rls_engine",
    "install_org_guc_guard",
    "org_scope",
]
