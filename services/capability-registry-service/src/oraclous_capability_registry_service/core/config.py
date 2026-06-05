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

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str
    INTERNAL_SERVICE_KEY: str
    AUTH_SERVICE_URL: str = "http://auth-service:8000"
    APP_NAME: str = "oraclous-capability-registry-service"
    VERSION: str = "0.0.1"

    # --- credential-broker seam (tool execution resolves credentials here; never decrypts) ---
    CREDENTIAL_BROKER_URL: str = "http://credential-broker-service:8000"
    CREDENTIAL_BROKER_MODE: str = "fake"  # "fake" (dev/CI, key-free) | "real"
    # Fake-broker connection string for relational connectors; defaults to this service's own DB so
    # the PostgreSQL connector runs a real query in the key-free smoke. Override to point elsewhere.
    FAKE_DB_DSN: str | None = None

    # --- identity seam (dev-auth by default; `jwt` consumes the real auth-service token) ---
    AUTH_MODE: str = "dev"  # "dev" | "jwt"
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
