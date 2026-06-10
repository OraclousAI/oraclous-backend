"""capability-registry-service settings (ORAA-4 §21 core layer).

``INTERNAL_SERVICE_KEY`` has no default: the service fails closed when absent
(Structured Threat Catalogue T6, ADR-008). The identity seam mirrors the
KGS/KRS/credential-broker pattern — ``dev`` mode binds a fixed principal+org from
a fixed bearer; ``jwt`` mode decodes the real HS256 auth-service token with the
shared ``JWT_SECRET`` per the JWT/Principal Contract. Settings are constructed
lazily via ``get_settings`` so importing this module never requires the
environment to be populated.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str
    INTERNAL_SERVICE_KEY: str
    AUTH_SERVICE_URL: str = "http://auth-service:8000"
    APP_NAME: str = "oraclous-capability-registry-service"
    VERSION: str = "0.0.1"

    # The org that owns the built-in/platform tool catalogue (global tools). Distinct from
    # ``DEV_ORG_ID``: the catalogue is seeded under this org and every tenant org reads it (widened
    # reads), so a freshly-provisioned org sees the platform tools without per-org re-seeding.
    PLATFORM_ORG_ID: str = "00000000-0000-0000-0000-0000000000a0"

    # --- knowledge-retriever seam (the first-party retriever tool POSTs to its /v1/search/*) ---
    # No credential: the retriever is reached over the internal/gateway-trust path (ADR-018), the
    # caller's org identity forwarded as X-Principal-*/X-Organisation-Id gated by X-Internal-Key.
    KNOWLEDGE_RETRIEVER_URL: str = "http://knowledge-retriever-service:8000"

    # --- credential-broker seam (tool execution resolves credentials here; never decrypts) ---
    CREDENTIAL_BROKER_URL: str = "http://credential-broker-service:8000"
    CREDENTIAL_BROKER_MODE: str = "fake"  # "fake" (dev/CI, key-free) | "real"
    # Fake-broker connection string for relational connectors; defaults to this service's own DB so
    # the PostgreSQL connector runs a real query in the key-free smoke. Override to point elsewhere.
    FAKE_DB_DSN: str | None = None

    # --- identity seam. `gateway` (production, ADR-018): trust the gateway's verified
    # X-Principal-*/X-Organisation-Id headers, gated by X-Internal-Key — no token validation here.
    # `dev`: a fixed bearer → fixed dev principal+org. `jwt`: decode a real HS256 auth-service token
    # directly (used when a caller reaches the service without the gateway). ---
    AUTH_MODE: Literal["gateway", "dev", "jwt"] = "dev"
    DEV_BEARER: str = "dev-token"
    DEV_USER_ID: str = "00000000-0000-0000-0000-0000000000c5"
    DEV_ORG_ID: str = "00000000-0000-0000-0000-00000000050a"
    JWT_SECRET: str | None = None
    JWT_ALGORITHM: str = "HS256"

    @property
    def sync_database_url(self) -> str:
        """psycopg (sync) DSN derived from the async one — used by Alembic."""
        return self.DATABASE_URL.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
