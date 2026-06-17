"""Unit: gateway settings — CORS origins parsing + upstream defaults."""

from __future__ import annotations

import pytest
from oraclous_application_gateway_service.core.config import Settings

pytestmark = pytest.mark.unit


def test_cors_origins_splits_comma_separated() -> None:
    s = Settings(GATEWAY_CORS_ORIGINS="https://a.test, https://b.test ,")
    assert s.cors_origins == ["https://a.test", "https://b.test"]


def test_cors_origins_wildcard_default() -> None:
    assert Settings().cors_origins == ["*"]


def test_upstream_defaults_point_at_substrate_service_names() -> None:
    s = Settings()
    assert s.AUTH_SERVICE_URL.endswith("auth-service:8000")
    assert s.CAPABILITY_REGISTRY_URL.endswith("capability-registry-service:8000")
    assert s.GATEWAY_AUTH_MODE == "dev"


def test_owner_database_url_defaults_to_database_url() -> None:
    """Single-DSN dev/test: the OWNER engine DSN defaults to DATABASE_URL (both are the owner, RLS
    a no-op) so behaviour is unchanged until the deploy sets a split OWNER_DATABASE_URL."""
    s = Settings(DATABASE_URL="postgresql+asyncpg://oraclous:oraclous@h/db")
    assert s.owner_database_url == "postgresql+asyncpg://oraclous:oraclous@h/db"


def test_owner_database_url_overrides_when_split() -> None:
    """The deployed RLS stack flips DATABASE_URL to oraclous_app while OWNER_DATABASE_URL stays the
    owner — only the two pre-auth producer reads use the owner."""
    s = Settings(
        DATABASE_URL="postgresql+asyncpg://oraclous_app:app@h/db",
        OWNER_DATABASE_URL="postgresql+asyncpg://oraclous:oraclous@h/db",
    )
    assert s.owner_database_url == "postgresql+asyncpg://oraclous:oraclous@h/db"


def test_sync_database_url_is_always_the_owner_psycopg_dsn() -> None:
    """Alembic + the rls-role bootstrap run as the owner: sync_database_url derives from the OWNER
    DSN and swaps asyncpg→psycopg, even when the org-bound DATABASE_URL is the oraclous_app role."""
    s = Settings(
        DATABASE_URL="postgresql+asyncpg://oraclous_app:app@h/db",
        OWNER_DATABASE_URL="postgresql+asyncpg://oraclous:oraclous@h/db",
    )
    assert s.sync_database_url == "postgresql+psycopg://oraclous:oraclous@h/db"


def test_rls_assert_runtime_role_defaults_off() -> None:
    """Default false so a test/local run on the owner DSN need not provision the app role."""
    assert Settings().GATEWAY_RLS_ASSERT_RUNTIME_ROLE is False
