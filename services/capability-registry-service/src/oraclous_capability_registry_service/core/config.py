"""capability-registry-service settings (R2 service shell).

INTERNAL_SERVICE_KEY has no default: the service fails closed when absent
(Structured Threat Catalogue T6, ADR-008). Settings are constructed lazily
via ``get_settings`` so importing this module never requires the environment
to be populated.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://oraclous:oraclous@localhost/oraclous"
    INTERNAL_SERVICE_KEY: str = ""
    AUTH_SERVICE_URL: str = "http://auth-service:8000"
    APP_NAME: str = "oraclous-capability-registry-service"
    VERSION: str = "0.0.0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
