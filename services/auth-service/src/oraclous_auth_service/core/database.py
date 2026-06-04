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
    create_async_engine,
)

from oraclous_auth_service.core.config import get_settings


def make_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_pre_ping=True, future=True)


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
