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

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str
    ENCRYPTION_KEY: str
    AUTH_SERVICE_URL: str = "http://auth-service:8000"
    INTERNAL_SERVICE_KEY: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
