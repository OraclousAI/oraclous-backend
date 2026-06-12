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

import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
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
    principal_from_gateway_headers,
    verify_token,
)
from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.core.database import session_scope
from oraclous_knowledge_graph_service.repositories.community_repository import CommunityRepository
from oraclous_knowledge_graph_service.repositories.graph_repository import GraphRepository
from oraclous_knowledge_graph_service.repositories.graph_write_repository import (
    GraphWriteRepository,
)
from oraclous_knowledge_graph_service.repositories.job_repository import IngestionJobRepository
from oraclous_knowledge_graph_service.repositories.recipe_repository import RecipeRepository
from oraclous_knowledge_graph_service.repositories.resolution_repository import ResolutionRepository
from oraclous_knowledge_graph_service.services.analytics_service import AnalyticsService
from oraclous_knowledge_graph_service.services.community_summarizer import make_summarizer
from oraclous_knowledge_graph_service.services.dry_run_service import DryRunService
from oraclous_knowledge_graph_service.services.graph_service import GraphService
from oraclous_knowledge_graph_service.services.job_service import JobService
from oraclous_knowledge_graph_service.services.ontology_service import OntologyService
from oraclous_knowledge_graph_service.services.recipe_service import RecipeService
from oraclous_knowledge_graph_service.services.recipes.engine import get_recipe_engine
from oraclous_knowledge_graph_service.services.resolution_service import ResolutionService

_bearer = HTTPBearer(auto_error=False)


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    """The app-scoped sessionmaker built in `core/lifespan` and stored on app.state."""
    return request.app.state.sessionmaker


async def get_db_session(
    maker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> AsyncIterator[AsyncSession]:
    async for session in session_scope(maker):
        yield session


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _require_internal_key(provided: str | None) -> None:
    """Fail-closed: a gateway-mode request must carry the shared X-Internal-Key (constant-time)."""
    expected = get_settings().internal_service_key
    if not expected or not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="request did not originate at the gateway"
        )


async def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    x_principal_id: Annotated[str | None, Header()] = None,
    x_principal_type: Annotated[str | None, Header()] = None,
    x_organisation_id: Annotated[str | None, Header()] = None,
    x_internal_key: Annotated[str | None, Header()] = None,
) -> Principal:
    settings = get_settings()
    # gateway mode (ADR-018): trust the gateway's verified identity headers + X-Internal-Key.
    if settings.auth_mode == "gateway":
        _require_internal_key(x_internal_key)
        try:
            return principal_from_gateway_headers(
                x_principal_id, x_principal_type, x_organisation_id
            )
        except AuthError as exc:
            raise _unauthorized(str(exc)) from exc
    # dev / jwt modes: resolve the bearer token directly.
    if credentials is None:
        raise _unauthorized("missing bearer token")
    try:
        return await verify_token(credentials.credentials)
    except AuthError as exc:
        raise _unauthorized(str(exc)) from exc


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
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _org: Annotated[OrganisationContext, Depends(bind_org_context)],
) -> GraphService:
    """Build the graph use-case service. Depends on `bind_org_context` so the org scope is
    already bound before any repository query runs.

    Wires the Neo4j-backed write repo when the substrate is configured, so GraphResponse counts
    reflect the LIVE Neo4j node/relationship counts (the Postgres columns are stale). Resolved off
    app.state directly — NOT via `get_neo4j_driver` — so graph CRUD never 503s when Neo4j is
    unconfigured; in that case `write_repo` is None and the service falls back to the stored
    Postgres columns.
    """
    driver = getattr(request.app.state, "neo4j_driver", None)
    write_repo = (
        GraphWriteRepository(driver, database=get_settings().neo4j_database)
        if driver is not None
        else None
    )
    return GraphService(GraphRepository(session), write_repo)


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


def get_resolution_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    graph_service: Annotated[GraphService, Depends(get_graph_service)],
    write_repo: Annotated[GraphWriteRepository, Depends(get_graph_write_repo)],
) -> ResolutionService:
    """Build the HITL resolution service. `graph_service` carries the owner gate + the bound org
    scope; `write_repo` (via `get_neo4j_driver`) is the Neo4j mutation surface — so the endpoint
    503s when the substrate is down (a mutation cannot proceed without it). The audit log is the
    Postgres `entity_resolutions` table."""
    return ResolutionService(
        graph_service=graph_service,
        write_repo=write_repo,
        audit_repo=ResolutionRepository(session),
    )


def _enqueue_detect(job_id: str, organisation_id: str) -> None:
    # Lazy import: keep the Celery app out of the request module's import graph.
    from oraclous_knowledge_graph_service.tasks.community_tasks import detect_communities_task

    detect_communities_task.delay(job_id, organisation_id)


def get_analytics_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    graph_service: Annotated[GraphService, Depends(get_graph_service)],
    driver: Annotated[Driver, Depends(get_neo4j_driver)],
) -> AnalyticsService:
    """Build the community-detection + analytics service (#303). `graph_service` carries the owner
    gate + bound org scope; the Neo4j-backed `CommunityRepository` is the GDS access surface (so the
    endpoint 503s when the substrate is down — detection needs it). The repo gets the app-scoped
    advisory Redis lock client so the inline detect shares the per-(org,graph) mutex with workers.
    The async detect path reuses the `ingestion_jobs` table + Celery worker; the summarizer is built
    from config (None when `KGS_EXTRACTOR` is not `openai`, so the summarize endpoint 503s with a
    clear reason).
    """
    settings = get_settings()
    repo = CommunityRepository(
        driver,
        database=settings.neo4j_database,
        lock_client=getattr(request.app.state, "detect_lock_client", None),
    )
    return AnalyticsService(
        graph_service=graph_service,
        repo=repo,
        job_repo=IngestionJobRepository(session),
        enqueue=_enqueue_detect,
        summarizer=make_summarizer(settings, repo=repo),
    )


def get_recipe_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _org: Annotated[OrganisationContext, Depends(bind_org_context)],
) -> RecipeService:
    return RecipeService(RecipeRepository(session), get_recipe_engine())


def get_ontology_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    graph_service: Annotated[GraphService, Depends(get_graph_service)],
) -> OntologyService:
    return OntologyService(GraphRepository(session), graph_service)


def get_dry_run_service() -> DryRunService:
    """The recipe dry-run authoring aid (Slice C). Pure: it writes NOTHING to Neo4j, so it needs no
    org binding or DB session — only an authenticated caller (the route depends on UserIdDep)."""
    return DryRunService()


# Public dependency aliases for route signatures.
GraphServiceDep = Annotated[GraphService, Depends(get_graph_service)]
UserIdDep = Annotated[uuid.UUID, Depends(get_current_user_id)]
JobServiceDep = Annotated[JobService, Depends(get_job_service)]
GraphWriteRepoDep = Annotated[GraphWriteRepository, Depends(get_graph_write_repo)]
RecipeServiceDep = Annotated[RecipeService, Depends(get_recipe_service)]
ResolutionServiceDep = Annotated[ResolutionService, Depends(get_resolution_service)]
AnalyticsServiceDep = Annotated[AnalyticsService, Depends(get_analytics_service)]
OntologyServiceDep = Annotated[OntologyService, Depends(get_ontology_service)]
DryRunServiceDep = Annotated[DryRunService, Depends(get_dry_run_service)]
