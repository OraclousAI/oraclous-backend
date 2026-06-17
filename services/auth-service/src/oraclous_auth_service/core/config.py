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

from oraclous_governance import require_secret

# Dev-only fallbacks (used IFF RUN_MODE != prod). In prod (RUN_MODE=prod) a missing/empty value for
# any of these raises at get_settings() construction (fail closed, T6 / ADR-008) — these strings are
# publicly-known and must never reach production. See oraclous_governance.require_secret.
_DEV_JWT_SECRET = "change-me-in-production"  # noqa: S105 — dev default, gated by RUN_MODE
_DEV_INTERNAL_SERVICE_KEY = "dev-internal-key"  # noqa: S105 — dev default, gated by RUN_MODE


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
    credential_broker_url: str
    # The DSN the identity engine (users/orgs/oauth/members/invitations/refresh/audit) connects on.
    # Defaults to ``database_url``; the deployed runtime overrides it to the NOSUPERUSER app
    # role (ADR-030 §3) so the no-bound-org login flows prove they run under the RLS runtime role.
    # The credential store (agents/agent_credentials) deliberately stays on ``database_url`` (owner)
    # — it is the ADR-012 §1a org-context PRODUCER (pre-auth global resolve), so it must NOT be
    # org-scoped/RLS-enforced on its connection; RLS there is the backstop proven by the test.
    identity_database_url: str
    # Postgres RLS backstop (ADR-030 Slice 1). When true, the identity engine asserts at startup
    # that its runtime DB role is NOSUPERUSER/NOBYPASSRLS (a bypassing role silently voids the RLS
    # policy on agents/agent_credentials — T1-M3) and FAILS CLOSED otherwise. The deployed runtime
    # connects the identity engine as oraclous_app with this on; migrations + the owner-run grant
    # bootstrap keep using the owner (superuser) DSN and never set this. Default false so a
    # test/local run that intentionally uses the owner DSN is not forced to provision the app role.
    rls_assert_runtime_role: bool

    @property
    def sync_database_url(self) -> str:
        """psycopg (sync) DSN derived from the async one — used by Alembic + seed_dev."""
        return self.database_url.replace("+asyncpg", "+psycopg")


def get_settings() -> Settings:
    """Return a freshly resolved :class:`Settings` snapshot."""
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://oraclous:oraclous@postgres:5432/oraclous"
    )
    return Settings(
        jwt_secret=require_secret("JWT_SECRET", dev_default=_DEV_JWT_SECRET),
        jwt_algorithm=os.environ.get("JWT_ALGORITHM", "HS256"),
        # 15-minute cap mirrors the legacy ``_SA_TOKEN_EXPIRE_MINUTES`` and is
        # pinned by ``test_agent_token_is_short_lived_capped_at_fifteen_minutes``.
        agent_token_ttl_minutes=int(os.environ.get("AGENT_TOKEN_TTL_MINUTES", "15")),
        user_access_token_ttl_minutes=int(os.environ.get("USER_ACCESS_TOKEN_TTL_MINUTES", "30")),
        refresh_token_ttl_days=int(os.environ.get("REFRESH_TOKEN_TTL_DAYS", "14")),
        database_url=database_url,
        # The identity engine's DSN; defaults to the owner DATABASE_URL when unset (dev/test/local
        # run on the owner). The deployed runtime sets it to the oraclous_app role.
        identity_database_url=os.environ.get("AUTH_IDENTITY_DATABASE_URL", database_url),
        redis_url=os.environ.get("REDIS_URL", "redis://redis:6379/0"),
        internal_service_key=require_secret(
            "INTERNAL_SERVICE_KEY", dev_default=_DEV_INTERNAL_SERVICE_KEY
        ),
        credential_broker_url=os.environ.get(
            "CREDENTIAL_BROKER_URL", "http://credential-broker-service:8000"
        ),
        rls_assert_runtime_role=os.environ.get("RLS_ASSERT_RUNTIME_ROLE", "").lower()
        in ("1", "true", "yes"),
    )
