"""Provision the NOSUPERUSER/NOBYPASSRLS runtime role + GRANTs (ADR-030 §3, core connection layer).

    python -m oraclous_credential_broker_service.core.bootstrap_rls_role

Run as the PRIVILEGED OWNER (the ``oraclous`` superuser) by the migrate one-shot, AFTER
``alembic upgrade head`` has created the tables. Idempotent + re-runnable:

* ``CREATE ROLE oraclous_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE`` if absent.
* ``GRANT USAGE ON SCHEMA public`` and ``GRANT SELECT, INSERT, UPDATE, DELETE`` on the four
  org-scoped broker tables to ``oraclous_app`` (and on FUTURE tables via ALTER DEFAULT PRIVILEGES so
  a later migration's tables are covered without re-running this with new names).

The runtime credential-broker then connects as ``oraclous_app`` (a non-owner, NOSUPERUSER role), so
the FORCE'd RLS policy bites it. The OWNER keeps running migrations + the operator backfill (it
bypasses RLS as a superuser). This is the riskiest step in the rollout — a missing GRANT fails a
service closed at first query — so the grants are provisioned in lockstep with the RLS migration and
the runtime asserts its role at startup (``RLS_ASSERT_RUNTIME_ROLE``).

Why a separate bootstrap rather than only the Alembic migration: a versioned migration runs once,
but the role/GRANTs must survive a fresh ``oraclous_app`` (e.g. role dropped) and re-apply every
deploy — so this idempotent step runs unconditionally in the migrate one-shot, complementing the
init script that covers a fresh ``pgdata`` volume.
"""

from __future__ import annotations

from oraclous_credential_broker_service.core.config import get_settings

# The four org-scoped broker tables RLS is enabled on (0004_enable_rls). The runtime role needs DML
# on exactly these (no sequences — all PKs are client-generated UUIDs).
_RLS_TABLES = ("user_credentials", "webhook_secrets", "delegated_tokens", "org_data_keys")

# Dev/self-host runtime role + password. Production overrides the runtime DSN with a managed
# credential; this default keeps the dev docker stack key-free and matches the compose runtime DSN
# and the integration-test fixtures.
_APP_ROLE = "oraclous_app"
_APP_PASSWORD = "app"  # noqa: S105 — dev/self-host default, overridden in production deploys


def _ddl() -> list[str]:
    role = _APP_ROLE
    grants = [
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON public."{t}" TO {role}' for t in _RLS_TABLES
    ]
    return [
        # idempotent role create (NOSUPERUSER NOBYPASSRLS so the RLS policy binds — ADR-030 §3)
        f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN "
        f"CREATE ROLE {role} LOGIN PASSWORD '{_APP_PASSWORD}' "
        "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE; END IF; END $$;",
        f"GRANT USAGE ON SCHEMA public TO {role}",
        *grants,
        # cover any future broker table created by a later migration without re-listing it here
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}",
    ]


def main() -> None:
    import psycopg

    dsn = get_settings().sync_database_url
    # psycopg wants a libpq DSN, not a SQLAlchemy URL — strip the +psycopg driver tag.
    libpq_dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(libpq_dsn, autocommit=True) as conn, conn.cursor() as cur:
        for stmt in _ddl():
            cur.execute(stmt)  # only trusted module constants are executed
    print(  # noqa: T201 — one-shot CLI output
        f"rls-role bootstrap complete: {_APP_ROLE} provisioned + granted on {list(_RLS_TABLES)}"
    )


if __name__ == "__main__":
    main()
