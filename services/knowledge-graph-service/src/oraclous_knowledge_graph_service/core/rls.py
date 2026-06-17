"""Postgres RLS wiring for the knowledge-graph-service (ADR-030 / #353, core layer).

The RLS seam is canonical in the substrate (``oraclous_substrate.access_async``); this module is a
thin **re-export shim** so the service's local import surface (``from ...core.rls import
install_org_guc_guard, assert_runtime_role_isolates``) is unchanged while the implementation lives
in exactly one place across services (ADR-030 §2). It activates the row-level-security backstop for
the KGS's four org-scoped Postgres tables (``knowledge_graphs``, ``ingestion_jobs``, ``recipes``,
``entity_resolutions``):

* :func:`install_org_guc_guard` — register the ``begin``-event guard on an ``AsyncEngine`` so
  **every** transaction on that engine binds ``app.current_organisation_id`` transaction-locally
  from the bound ``OrganisationContext`` (and fails closed to the empty GUC — zero rows — when no
  context is bound). Both KGS engine factories (``core/database.make_engine`` for the web path AND
  ``make_worker_engine`` for the per-task Celery worker) install it: the web request binds the org
  via ``bind_org_context`` (a FastAPI dependency) and the worker binds it via
  ``use_organisation_context`` before any query, so the guard reads that already-bound org. The KGS
  does not build engines through a single ``build_rls_engine`` call site (the web engine is
  pool-tuned, the worker engine is NullPool), so it installs the guard directly on each — equivalent
  to ``build_rls_engine`` for each factory.

* :func:`assert_runtime_role_isolates` — startup fail-closed: refuse to come up under a superuser /
  ``BYPASSRLS`` role, which would silently void the backstop (T1-M3). It is the substrate
  ``assert_non_bypassing_role`` re-exported under the service-local name the lifespan imports.

The privileged operator paths — the Alembic migrations and the dev-org seed
(``scripts/seed_dev.py``, which writes a ``knowledge_graphs`` row with NO bound org) — connect on
the **owner** DSN, a superuser in the dev stack that therefore bypasses RLS; the empty GUC the guard
would bind there is irrelevant under an owner/superuser role. Only the long-running web service +
the Celery worker switch their DSN to the NOSUPERUSER ``oraclous_app`` role (ADR-030 §3).
"""

from __future__ import annotations

from oraclous_substrate.access_async import (
    RlsBypassingRoleError,
    assert_non_bypassing_role,
    install_org_guc_guard,
    org_scope,
)

# Service-local name the lifespan imports; the substrate assertion is the implementation.
assert_runtime_role_isolates = assert_non_bypassing_role

__all__ = [
    "RlsBypassingRoleError",
    "assert_runtime_role_isolates",
    "install_org_guc_guard",
    "org_scope",
]
