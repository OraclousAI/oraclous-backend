"""Provision the NOSUPERUSER/NOBYPASSRLS runtime role + GRANTs (ADR-030 §3, core connection layer).

    python -m oraclous_auth_service.core.bootstrap_rls_role

Run as the PRIVILEGED OWNER (the ``oraclous`` superuser) by the migrate one-shot, AFTER
``alembic upgrade head`` has created the tables. Idempotent + re-runnable:

* ``CREATE ROLE oraclous_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE`` if absent.
* ``GRANT USAGE ON SCHEMA public`` and ``GRANT SELECT, INSERT, UPDATE, DELETE`` on **all** of auth's
  tables to ``oraclous_app`` (and on FUTURE tables via ALTER DEFAULT PRIVILEGES). The grant is broad
  (all tables) — NOT just the two RLS-enabled ones — because the **identity engine** runs as
  ``oraclous_app`` and reads/writes every identity table (users, organisations, org_members, the
  oauth tables, refresh_tokens, org_invitations, auth_audit_log); a grant limited to the RLS pair
  would fail those flows closed at first query (``permission denied for table users``). Those
  identity tables are NOT RLS-enabled (they are reached before an org is bound — see rls_coverage
  exclusions), so the broad grant is the only control there; RLS scopes the two enrolled tables on
  top of it. The grant order is safe regardless of who else granted: GRANT is additive + idempotent.

The IDENTITY engine connects as ``oraclous_app`` (a non-owner, NOSUPERUSER role) + asserts it at
startup; the credential store (``agents`` / ``agent_credentials``) is the ADR-012 §1a org-context
PRODUCER and stays on the OWNER DSN for its pre-auth global resolves (so RLS does not fail-close
them). The OWNER also runs migrations + this bootstrap (it bypasses RLS as a superuser). This is the
riskiest step in the rollout — a missing GRANT fails the identity engine closed at first query — so
the grants are provisioned in lockstep with the RLS migration and the runtime asserts its role at
startup (``RLS_ASSERT_RUNTIME_ROLE``).

Why a separate bootstrap rather than only the Alembic migration: a versioned migration runs once,
but the role/GRANTs must survive a fresh ``oraclous_app`` (e.g. role dropped) and re-apply every
deploy — so this idempotent step runs unconditionally in the migrate one-shot, complementing the
init script that covers a fresh ``pgdata`` volume.
"""

from __future__ import annotations

from oraclous_substrate.access_async import provision_app_role

from oraclous_auth_service.core.config import get_settings

# Auth's always-org-bound tables RLS is enabled on (0007_enable_rls) — recorded here for the
# completion message + as the documented RLS scope (the grant below is broader: all tables, because
# the identity engine touches them all).
_RLS_TABLES = ("agents", "agent_credentials")

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
        # grant_all_tables=True: ALL tables, not just the RLS pair — the identity engine runs as
        # oraclous_app and reads/writes every identity table (users/organisations/org_members/
        # oauth_*/refresh_tokens/invitations/audit); a narrower grant fails login closed (permission
        # denied for table users). RLS scopes the two enrolled tables on top of this broad grant;
        # the rest are not RLS-d (reached before an org is bound). _RLS_TABLES passed for docs.
        provision_app_role(
            conn,
            role=_APP_ROLE,
            password=_APP_PASSWORD,
            tables=_RLS_TABLES,
            grant_all_tables=True,
        )
    print(  # noqa: T201 — one-shot CLI output
        f"rls-role bootstrap complete: {_APP_ROLE} provisioned + granted (RLS: {list(_RLS_TABLES)})"
    )


if __name__ == "__main__":
    main()
