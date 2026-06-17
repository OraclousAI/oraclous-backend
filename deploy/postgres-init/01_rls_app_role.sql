-- ADR-030 §3 — provision the NOSUPERUSER/NOBYPASSRLS runtime role for the Postgres RLS backstop.
--
-- Mounted at /docker-entrypoint-initdb.d/ so postgres runs it ONCE on a FRESH `pgdata` volume
-- (the entrypoint only runs init scripts when the data dir is empty). It creates the role up front
-- so a from-scratch `docker compose up` has `oraclous_app` before any runtime service connects as it.
--
-- Table-level GRANTs are NOT here (the tables don't exist yet at init time) — each realized service's
-- migrate one-shot runs `python -m ...core.bootstrap_rls_role` after `alembic upgrade head`, which
-- (idempotently) re-creates this role if missing AND grants DML on that service's org-scoped tables.
-- So this script is the fresh-volume fast path; the bootstrap step is the authoritative, re-runnable
-- provisioner that also covers an existing volume and the per-table grants.
--
-- The runtime connects as oraclous_app; the owner (`oraclous`, superuser) keeps running migrations +
-- the operator backfill (which bypasses RLS). Dev/self-host password is `app` (matches the compose
-- runtime DSN + the integration-test fixtures); production overrides the runtime DSN with a managed
-- credential.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'oraclous_app') THEN
    CREATE ROLE oraclous_app LOGIN PASSWORD 'app'
      NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO oraclous_app;

-- Cover tables created LATER (by each service's migrations) so the runtime role can DML them as soon
-- as they exist, even before the per-service bootstrap re-grants. RLS still scopes every row.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO oraclous_app;
