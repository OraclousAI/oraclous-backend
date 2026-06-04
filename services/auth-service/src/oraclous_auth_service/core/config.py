"""Auth-service settings loader (ORA-31 · extended R3.5-P3-S1).

Reads JWT, Postgres, Redis and internal-key config from the environment. Kept as a frozen dataclass
recomputed on every :func:`get_settings` call (NOT lru-cached) so the agent-token tests can mutate
``os.environ`` between calls — production should treat ``get_settings()`` as cheap and call it where
the value is needed rather than caching it at import (which would freeze the test secret).

Env var convention is **unprefixed** (``JWT_SECRET``, ``DATABASE_URL``, ``INTERNAL_SERVICE_KEY``) —
auth-service is the Substrate identity service, and the shared ``JWT_SECRET`` is injected verbatim
into the KGS/KRS verifiers (as ``KGS_JWT_SECRET`` / ``KRS_JWT_SECRET``) by docker-compose so issue
and verify share one secret.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    """Auth-service configuration resolved from the environment."""

    jwt_secret: str
    jwt_algorithm: str
    agent_token_ttl_minutes: int
    user_access_token_ttl_minutes: int
    refresh_token_ttl_days: int
    database_url: str
    redis_url: str
    internal_service_key: str

    @property
    def sync_database_url(self) -> str:
        """psycopg (sync) DSN derived from the async one — used by Alembic + seed_dev."""
        return self.database_url.replace("+asyncpg", "+psycopg")


def get_settings() -> Settings:
    """Return a freshly resolved :class:`Settings` snapshot."""
    return Settings(
        jwt_secret=os.environ.get("JWT_SECRET", "change-me-in-production"),
        jwt_algorithm=os.environ.get("JWT_ALGORITHM", "HS256"),
        # 15-minute cap mirrors the legacy ``_SA_TOKEN_EXPIRE_MINUTES`` and is
        # pinned by ``test_agent_token_is_short_lived_capped_at_fifteen_minutes``.
        agent_token_ttl_minutes=int(os.environ.get("AGENT_TOKEN_TTL_MINUTES", "15")),
        user_access_token_ttl_minutes=int(os.environ.get("USER_ACCESS_TOKEN_TTL_MINUTES", "30")),
        refresh_token_ttl_days=int(os.environ.get("REFRESH_TOKEN_TTL_DAYS", "14")),
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://oraclous:oraclous@postgres:5432/oraclous"
        ),
        redis_url=os.environ.get("REDIS_URL", "redis://redis:6379/0"),
        internal_service_key=os.environ.get("INTERNAL_SERVICE_KEY", "dev-internal-key"),
    )
