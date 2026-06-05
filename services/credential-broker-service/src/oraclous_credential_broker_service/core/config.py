"""Credential-broker settings (reshape of legacy ``app/core/config.py``).

The internal service key has **no default**: it is sourced from secret
management (the environment / a secret manager) and the service fails closed if
it is absent, rather than starting with a baked-in, publicly-known key
(Structured Threat Catalogue T6, ADR-008). Settings are constructed lazily via
``get_settings`` so importing this module never requires the environment to be
populated.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str
    ENCRYPTION_KEY: str
    AUTH_SERVICE_URL: str = "http://auth-service:8000"
    INTERNAL_SERVICE_KEY: str

    # --- identity seam (dev-auth by default). `gateway` (production, ADR-018): trust the gateway's
    # verified X-Principal-*/X-Organisation-Id headers, gated by the existing X-Internal-Key — no
    # token validation at the edge. `dev`: a fixed bearer → fixed dev principal+org. `jwt`: consume
    # the real auth-service HS256 token directly. ---
    AUTH_MODE: Literal["gateway", "dev", "jwt"] = "dev"
    DEV_BEARER: str = "dev-token"
    DEV_USER_ID: str = "00000000-0000-0000-0000-0000000000d5"
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
