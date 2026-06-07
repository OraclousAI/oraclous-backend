"""Service configuration (ORAA-4 §21 core layer) — env → Settings (execution engine, R5).

The execution engine is a Layer-3 orchestrator: it runs harnesses as durable jobs, schedules them,
manages the human task board, and resumes paused runs — calling the harness-runtime over HTTP (never
importing it; four-layer contract). It owns a small Postgres store (job rows + provenance sink) +
Redis-backed Celery queue (worker/beat land in later slices). Identity follows the same seam as the
other services (gateway / dev / jwt); the resolved principal is forwarded downstream to the harness
(ADR-018) so org-scoping holds end-to-end.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ENGINE_", extra="ignore")

    # --- identity seam (ADR-018). `gateway`: trust the gateway's verified X-Principal-*/
    # X-Organisation-Id, gated by X-Internal-Key. `dev`: a fixed bearer for the standalone smoke.
    # `jwt`: decode a real auth-service token. `verify_token` keeps one signature for the swap. ---
    auth_mode: Literal["gateway", "dev", "jwt"] = "dev"
    dev_bearer: str = "dev-token"
    dev_user_id: str = "00000000-0000-0000-0000-0000000000e7"
    # matches the other services' DEV_ORG_ID so a standalone smoke shares one tenant.
    dev_org_id: str = "00000000-0000-0000-0000-00000000050a"
    internal_service_key: str | None = None
    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"

    # --- own store (Postgres): job rows + the provenance sink. No hardcoded prod secret. ---
    database_url: str = "postgresql+asyncpg://oraclous:oraclous@postgres:5432/oraclous"

    # --- the durable queue (Celery worker + beat land in S2/S5; the URL is read now). ---
    redis_url: str = "redis://redis:6379/0"

    # --- upstream the engine calls (over HTTP; never imported) ---
    harness_runtime_url: str = "http://harness-runtime-service:8000"
    # an out-of-request harness run can be long (an LLM loop) — generous default.
    harness_request_timeout: float = 600.0

    @property
    def sync_database_url(self) -> str:
        """The synchronous psycopg DSN Alembic uses (swaps the asyncpg driver for psycopg)."""
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
