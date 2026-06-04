"""DI providers (ORAA-4 §21 core layer) — wiring only, no business logic.

The org-scope binding lives here: `bind_org_context` resolves the caller's organisation from the
authenticated principal and binds it into the governance ContextVar for the duration of the
request (via `use_organisation_context`). Every repository read/write inside the handler then sees
that org through `oraclous_substrate.access.enforced_organisation_id()` — fail-closed if unbound.
Swapping the dev seam for the real identity service (R3.5-P3) means replacing `verify_token` and
the `StaticMembershipResolver` here; the handlers and repositories do not change.

Dependencies are exposed as `Annotated[...]` aliases (e.g. `GraphServiceDep`) so route signatures
stay terse and ruff-clean (no `Depends()` in argument defaults, B008).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from neo4j import Driver
from oraclous_governance import (
    OrganisationContext,
    Principal,
    resolve_organisation_context,
    use_organisation_context,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oraclous_knowledge_graph_service.core.auth import (
    AuthError,
    StaticMembershipResolver,
    verify_token,
)
from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.core.database import session_scope
from oraclous_knowledge_graph_service.repositories.graph_repository import GraphRepository
from oraclous_knowledge_graph_service.repositories.graph_write_repository import (
    GraphWriteRepository,
)
from oraclous_knowledge_graph_service.repositories.job_repository import IngestionJobRepository
from oraclous_knowledge_graph_service.services.graph_service import GraphService
from oraclous_knowledge_graph_service.services.job_service import JobService

_bearer = HTTPBearer(auto_error=False)


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    """The app-scoped sessionmaker built in `core/lifespan` and stored on app.state."""
    return request.app.state.sessionmaker


async def get_db_session(
    maker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> AsyncIterator[AsyncSession]:
    async for session in session_scope(maker):
        yield session


async def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await verify_token(credentials.credentials)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def bind_org_context(
    principal: Annotated[Principal, Depends(get_principal)],
) -> AsyncIterator[OrganisationContext]:
    """Resolve + bind the caller's organisation context for the request lifetime (fail-closed)."""
    resolver = StaticMembershipResolver(uuid.UUID(get_settings().dev_org_id))
    context = await resolve_organisation_context(principal, resolver=resolver)
    with use_organisation_context(context):
        yield context


def get_current_user_id(principal: Annotated[Principal, Depends(get_principal)]) -> uuid.UUID:
    return principal.principal_id


def get_graph_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _org: Annotated[OrganisationContext, Depends(bind_org_context)],
) -> GraphService:
    """Build the graph use-case service. Depends on `bind_org_context` so the org scope is
    already bound before any repository query runs."""
    return GraphService(GraphRepository(session))


def get_neo4j_driver(request: Request) -> Driver:
    """The app-scoped Neo4j driver opened in lifespan. 503 if the substrate is unconfigured/down."""
    driver = getattr(request.app.state, "neo4j_driver", None)
    if driver is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="knowledge graph store unavailable (KGS_NEO4J_URI not configured)",
        )
    return driver


def _enqueue_ingest(job_id: str, organisation_id: str) -> None:
    # Lazy import: keep the Celery app out of the request module's import graph.
    from oraclous_knowledge_graph_service.tasks.ingest_tasks import ingest_document_task

    ingest_document_task.delay(job_id, organisation_id)


def get_job_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    graph_service: Annotated[GraphService, Depends(get_graph_service)],
) -> JobService:
    """Build the ingestion-job service. `graph_service` already bound the org scope + session."""
    return JobService(
        job_repo=IngestionJobRepository(session),
        graph_service=graph_service,
        enqueue=_enqueue_ingest,
    )


def get_graph_write_repo(
    driver: Annotated[Driver, Depends(get_neo4j_driver)],
    _org: Annotated[OrganisationContext, Depends(bind_org_context)],
) -> GraphWriteRepository:
    return GraphWriteRepository(driver, database=get_settings().neo4j_database)


# Public dependency aliases for route signatures.
GraphServiceDep = Annotated[GraphService, Depends(get_graph_service)]
UserIdDep = Annotated[uuid.UUID, Depends(get_current_user_id)]
JobServiceDep = Annotated[JobService, Depends(get_job_service)]
GraphWriteRepoDep = Annotated[GraphWriteRepository, Depends(get_graph_write_repo)]
