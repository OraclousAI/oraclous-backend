"""Async Postgres engine + session (ORAA-4 §21 core layer).

Connections are opened here and in `core/lifespan`; the actual SQL lives in `repositories/`
(the only layer that touches a driver, per §21 rule 3).

ADR-030 (#353): BOTH engine factories install the substrate org-GUC guard (the `begin`-event that
binds `app.current_organisation_id` transaction-locally from the bound `OrganisationContext`,
failing closed to the empty GUC — zero rows — when none is bound). The web factory AND the per-task
worker factory are guarded: a missed factory would leave the worker writing under FORCE'd RLS with
an empty GUC (every write denied) or, if it bypassed, silently unscoped. The web request binds the
org via the `bind_org_context` dependency and the worker via `use_organisation_context` before any
query, so the guard reads that already-bound org on every transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from oraclous_substrate.access_async import install_org_guc_guard
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from oraclous_knowledge_graph_service.core.config import get_settings


def make_engine() -> AsyncEngine:
    # ADR-030: install the org-GUC guard so every request-path transaction binds the bound org.
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True, future=True)
    install_org_guc_guard(engine)
    return engine


def make_worker_engine() -> AsyncEngine:
    """Task-scoped engine for the Celery worker — NullPool (no shared pool in workers, ADR-012).

    A worker owns its connection per task and disposes it after; never share the request-path
    pool across the process boundary. ADR-030: the worker engine carries the SAME org-GUC guard as
    the web engine — the worker binds the task org via `use_organisation_context` before building
    this engine (see `tasks/ingest_tasks.py` et al.), so every per-task transaction binds that org.
    """
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool, future=True)
    install_org_guc_guard(engine)
    return engine


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
