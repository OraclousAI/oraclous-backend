"""Async Postgres engine + session (ORAA-4 §21 core layer — connection setup).

Connections are opened here and in `core/lifespan`; the actual SQL lives in `repositories/` (the
only layer that touches a driver, per §21 rule 3).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from oraclous_auth_service.core.config import get_settings
from oraclous_auth_service.core.rls import build_rls_engine


def make_engine() -> AsyncEngine:
    # ADR-030 Slice 1: the identity engine carries the substrate RLS org-GUC guard. Its own tables
    # (users/organisations/org_members/oauth/refresh_tokens/auth_audit_log/invitations) are NOT
    # RLS-enabled (they are reached in pre-org / cross-org / token-lookup flows — see rls_coverage
    # exclusions), so the guard merely binds the empty GUC there (a no-op for non-RLS tables). The
    # guard is wired uniformly so that if a future slice RLS-enables one of these tables behind a
    # bound org, the binding is already in place; the runtime DSN switches to the NOSUPERUSER
    # oraclous_app role (ADR-030 §3), under which only RLS-enabled tables are policy-scoped.
    return build_rls_engine(get_settings().identity_database_url, pool_pre_ping=True, future=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def session_scope(
    maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a session, committing on success and rolling back on error."""
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
