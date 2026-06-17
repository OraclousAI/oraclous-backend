"""uvicorn entrypoint (ORAA-4 §21) — production composition of the auth-service app.

Wires the real Postgres-backed agent repository + the user-identity lifespan (which opens the
sessionmaker + Redis) and exposes module-level ``app`` for
``uvicorn oraclous_auth_service.main:app``.
The Alembic one-shot (``alembic upgrade head``) creates the schema before the service serves.
"""

from __future__ import annotations

from fastapi import FastAPI

from oraclous_auth_service.app.factory import create_app
from oraclous_auth_service.core.config import get_settings
from oraclous_auth_service.core.lifespan import lifespan
from oraclous_auth_service.repositories.agent_repository import AgentRepository
from oraclous_auth_service.repositories.postgres_credential_store import PostgresCredentialStore


def build_app() -> FastAPI:
    settings = get_settings()
    # ADR-030 §2/§3: the credential store splits its DB access by pattern. The pre-auth cross-org
    # resolves (validate-by-prefix / org-resolve / agent-lifecycle revoke) run on the OWNER
    # ``database_url`` (they precede any org context and must resolve across orgs). The org-bound
    # CRUD (create / list-by-org / get-by-org / revoke-by-org) runs on the org-bound engine, which
    # connects as the NOSUPERUSER ``oraclous_app`` identity role (``identity_database_url``) and
    # carries the org-GUC guard — so the RLS policy on agents/agent_credentials BITES on the actual
    # runtime org-bound path (not just a synthetic probe). Migrations + the grant bootstrap keep
    # using the owner DSN.
    agent_repository = AgentRepository(
        PostgresCredentialStore(
            settings.database_url, org_bound_db_url=settings.identity_database_url
        )
    )
    return create_app(
        agent_repository=agent_repository,
        internal_service_key=settings.internal_service_key,
        lifespan=lifespan,
    )


app = build_app()
