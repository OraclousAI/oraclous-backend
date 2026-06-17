"""Provision the NOSUPERUSER/NOBYPASSRLS runtime role + GRANTs (ADR-030 §3, core connection layer).

    python -m oraclous_harness_runtime_service.core.bootstrap_rls_role

Run as the PRIVILEGED OWNER (the ``oraclous`` superuser) by the migrate one-shot, AFTER
``alembic upgrade head`` has created the tables + enabled RLS. Idempotent + re-runnable:

* ``CREATE ROLE oraclous_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE`` if absent.
* ``GRANT USAGE ON SCHEMA public`` and ``GRANT SELECT, INSERT, UPDATE, DELETE`` on the four
  org-scoped harness tables to ``oraclous_app`` (and on FUTURE tables via ALTER DEFAULT PRIVILEGES
  so a later migration's tables are covered without re-running this with new names).

The runtime harness then connects as ``oraclous_app`` (a non-owner, NOSUPERUSER role), so the
FORCE'd RLS policy bites it across all four repositories. The OWNER keeps running migrations (it
bypasses RLS as a superuser). The harness has no Celery worker (all DB access is in-request), so the
runtime role is the same one role across the whole service. This is the riskiest step in the
rollout — a missing GRANT fails a service closed at first query — so the grants are provisioned in
lockstep with the RLS migration and the runtime asserts its role at startup
(HARNESS_RLS_ASSERT_RUNTIME_ROLE).

Why a separate bootstrap rather than only the Alembic migration: a versioned migration runs once,
but the role/GRANTs must survive a fresh ``oraclous_app`` (e.g. role dropped) and re-apply every
deploy — so this idempotent step runs unconditionally in the migrate one-shot, complementing the
init script that covers a fresh ``pgdata`` volume.
"""

from __future__ import annotations

from oraclous_substrate.access_async import provision_app_role

from oraclous_harness_runtime_service.core.config import get_settings

# The four org-scoped harness tables RLS is enabled on (the next migration). The runtime role needs
# DML on exactly these (no sequences — all PKs are client-generated UUIDs).
_RLS_TABLES = (
    "harness_executions",
    "harness_checkpoints",
    "harness_assignments",
    "harness_provenance",
)

# Dev/self-host runtime role + password. Production overrides the runtime DSN with a managed
# credential; this default keeps the dev docker stack key-free and matches the compose runtime DSN
# and the integration-test fixtures.
_APP_ROLE = "oraclous_app"
_APP_PASSWORD = "app"  # noqa: S105 — dev/self-host default, overridden in production deploys


def main() -> None:
    import psycopg

    dsn = get_settings().sync_database_url
    # psycopg wants a libpq DSN, not a SQLAlchemy URL — strip the +psycopg driver tag.
    libpq_dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(libpq_dsn, autocommit=True) as conn:
        # Delegate the idempotent role-create + GRANTs to the shared substrate helper (ADR-030 §3).
        # grant_all_tables=False: per-table DML grants on just the four RLS-enabled harness tables.
        provision_app_role(
            conn,
            role=_APP_ROLE,
            password=_APP_PASSWORD,
            tables=_RLS_TABLES,
            grant_all_tables=False,
        )
    print(  # noqa: T201 — one-shot CLI output
        f"rls-role bootstrap complete: {_APP_ROLE} provisioned + granted on {list(_RLS_TABLES)}"
    )


if __name__ == "__main__":
    main()
