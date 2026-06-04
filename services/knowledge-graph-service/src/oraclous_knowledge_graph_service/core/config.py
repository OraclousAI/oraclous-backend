"""Service configuration (ORAA-4 §21 core layer) — env → Settings.

R3.5-P1-S1. Pydantic-settings; each knob is a `KGS_`-prefixed env var with a dev-friendly default
so the service runs from `docker compose` with no secrets (the dev-auth seam). This declares ONLY
what S1 uses — the dev identity seam and Postgres. Neo4j / Redis / embedder / extractor are added by
S2 when the ingestion path is wired (the Neo4j URI then comes from `KGS_NEO4J_URI` with no
hardcoded default, per ORAA-53).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KGS_", extra="ignore")

    # --- identity seam (S1 dev-auth / single-tenant; swapped for the real identity service) ---
    auth_mode: Literal["dev", "jwt"] = "dev"
    dev_bearer: str = "dev-token"
    dev_user_id: str = "00000000-0000-0000-0000-0000000000d5"
    dev_org_id: str = "00000000-0000-0000-0000-00000000050a"

    # --- Postgres (graph metadata + ingestion jobs) ---
    database_url: str = "postgresql+asyncpg://oraclous:oraclous@postgres:5432/oraclous"

    @property
    def sync_database_url(self) -> str:
        """psycopg3 (sync) DSN derived from the async one — used by Alembic + seed_dev."""
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
