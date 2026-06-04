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
    agent_repository = AgentRepository(PostgresCredentialStore(settings.database_url))
    return create_app(
        agent_repository=agent_repository,
        internal_service_key=settings.internal_service_key,
        lifespan=lifespan,
    )


app = build_app()
