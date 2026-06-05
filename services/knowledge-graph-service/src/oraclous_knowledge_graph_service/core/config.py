"""Service configuration (ORAA-4 §21 core layer) — env → Settings.

Pydantic-settings; each knob is a `KGS_`-prefixed env var with a dev-friendly default so the
service runs from `docker compose` with no secrets (the dev-auth + hashing-embedder seams). S2 adds
the ingestion surface: Neo4j (write role kgs_writer), Redis/Celery, and the embedder/extractor
seams. `neo4j_uri` has NO hardcoded default (ORAA-53) — it must come from `KGS_NEO4J_URI`; when
unset the service runs in graph-CRUD-only mode and ingestion endpoints report the missing substrate.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KGS_", extra="ignore")

    # --- identity seam. `gateway` (production, ADR-018): trust the gateway's verified
    # X-Principal-*/X-Organisation-Id headers, gated by X-Internal-Key — no token validation here.
    # `dev`: a fixed bearer for the standalone smoke. `jwt`: decode a real HS256 token directly
    # (used when a caller reaches the service without the gateway). ---
    auth_mode: Literal["gateway", "dev", "jwt"] = "dev"
    dev_bearer: str = "dev-token"
    dev_user_id: str = "00000000-0000-0000-0000-0000000000d5"
    dev_org_id: str = "00000000-0000-0000-0000-00000000050a"
    # gateway mode: the shared secret the gateway sends as X-Internal-Key; fail-closed if unset.
    internal_service_key: str | None = None
    # jwt mode: the shared HS256 secret the auth-service signs with (compose injects KGS_JWT_SECRET
    # = the auth-service JWT_SECRET). No default — jwt mode fails closed without it.
    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"

    # --- Postgres (graph metadata + ingestion jobs) ---
    database_url: str = "postgresql+asyncpg://oraclous:oraclous@postgres:5432/oraclous"

    # --- Neo4j (kgs_writer role, ORAA-53). No hardcoded URI default. ---
    neo4j_uri: str | None = None
    neo4j_user: str = "kgs_writer"
    neo4j_password: str = "kgs-writer-pass"  # noqa: S105 — dev default; prod injects via secret
    neo4j_database: str | None = None

    # --- Redis / Celery (async ingestion spine) ---
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None

    # --- ingestion seams (key-free defaults: deterministic hashing embedder, no LLM extraction) ---
    embedder: Literal["hashing", "openai"] = "hashing"
    embedding_dim: int = 512
    extractor: Literal["null", "openai"] = "null"
    openai_api_key: str | None = None

    @property
    def sync_database_url(self) -> str:
        """psycopg3 (sync) DSN derived from the async one — used by Alembic + seed_dev."""
        return self.database_url.replace("+asyncpg", "+psycopg")

    @property
    def celery_broker(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def celery_backend(self) -> str:
        return self.celery_result_backend or self.redis_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
