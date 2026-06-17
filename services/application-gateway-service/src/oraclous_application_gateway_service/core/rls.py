"""Postgres RLS wiring for the application-gateway-service (ADR-030 / #353, core layer).

The RLS seam is canonical in the substrate (``oraclous_substrate.access_async``); this module is a
thin **re-export shim** so the service's local import surface (``from ...core.rls import
build_rls_engine, org_scope, assert_runtime_role_isolates``) is unchanged while the implementation
lives in exactly one place across services (ADR-030 §2). It activates the row-level-security
backstop for the gateway's FIVE org-scoped Postgres tables (``published_agents``, ``chat_threads``,
``chat_messages``, ``integration_keys``, ``webhook_subscriptions``).

The gateway has the SAME split the auth-service did (ADR-012 §1a + ADR-030 §3): its CRUD path is
org-bound (every repo read/write filters ``organisation_id`` and binds the org via ``org_scope``),
**but TWO pre-auth PRODUCER lookups resolve BEFORE any org context**:

* ``IntegrationKeyRepository.get_by_prefix`` — a UNIQUE non-secret prefix → the single row whose org
  it PRODUCES (the inbound integration-key authz floor; there is no org yet, it is what we are about
  to mint).
* ``WebhookSubscriptionRepository.get_by_id`` — the opaque subscription id IS the bearer-less
  credential an inbound webhook presents; the row then asserts its own org to the engine.

Under FORCE'd RLS on the org-bound ``oraclous_app`` engine those two reads would fail closed to zero
rows (T1-M1) — breaking integration-key auth and inbound webhooks (the HARD RULE). So the DB access
is carved into TWO engines, mirroring the auth-service / execution-engine split:

* The **org-bound engine** — ``build_rls_engine(oraclous_app DSN)`` with the org-GUC guard
  installed. Used by ALL org-bound repo methods (key create/list/rotate/revoke; subscription
  create/list/delete; the published-agent + chat CRUD). RLS BITES here: the guard binds
  ``app.current_organisation_id`` transaction-locally from the bound ``OrganisationContext`` per
  transaction (fail-closed to the empty GUC — zero rows — when none is bound), and each org-bound
  method binds the org it received from authenticated context via ``org_scope`` so the begin-guard
  sees it (the capability-registry/engine lesson: an org-bound op that fails to bind reads zero rows
  + writes 42501).

* The **owner engine** — the OWNER DSN (a superuser in the dev stack, which BYPASSES RLS), with the
  guard DELIBERATELY NOT installed (``install_guard=False``). Used ONLY by the two pre-auth producer
  reads (``get_by_prefix`` / ``get_by_id``), which MUST resolve across orgs. They precede org
  context and stay UNBOUND on this engine (correct — there is no org to bind yet). RLS on those two
  tables is therefore the *backstop* (defense-in-depth) proven under ``oraclous_app`` by the
  data-layer isolation test, not enforced on the producer connection.

This module re-exports the shared wiring both reach for:

* :func:`build_rls_engine` — construct an ``AsyncEngine`` with the substrate ``begin``-event guard
  installed (the org-bound engine factory).

* :func:`org_scope` — bind the org for an enclosed DB op so the engine guard sets the GUC. Every
  org-bound repo method wraps its body in ``org_scope(organisation_id)`` with the org it received
  from authenticated context (never request input — T1-M1).

* :func:`assert_runtime_role_isolates` — startup fail-closed: refuse to come up under a superuser /
  ``BYPASSRLS`` role, which would silently void the backstop (T1-M3). It is the substrate
  ``assert_non_bypassing_role`` re-exported under the service-local name the lifespan imports. The
  gateway runs NO Celery/background worker that touches these tables (webhook ingress is synchronous
  — it proxies inbound to the engine over HTTP), so there is no worker_process_init mirror to add;
  the web lifespan assertion is the sole chokepoint.

The privileged operator path — the Alembic migrations + the owner-run grant bootstrap, AND the
two pre-auth producer reads — connects on the OWNER DSN (a superuser in the dev stack, which
bypasses RLS); the empty GUC there is irrelevant under an owner/superuser role.
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
