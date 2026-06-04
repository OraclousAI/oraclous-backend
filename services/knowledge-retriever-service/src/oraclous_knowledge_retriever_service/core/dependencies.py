"""DI providers (ORAA-4 §21 core layer) — wiring only.

`bind_org_context` resolves the caller's organisation from the authenticated principal and binds it
into the governance ContextVar for the request; every repository read then sees it via
`enforced_organisation_id()` (fail-closed). Exposed as `Annotated[...]` aliases (B008-clean)
route signatures.
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

from oraclous_knowledge_retriever_service.core.auth import (
    AuthError,
    StaticMembershipResolver,
    verify_token,
)
from oraclous_knowledge_retriever_service.core.config import get_settings
from oraclous_knowledge_retriever_service.services.embedder import HashingEmbedder
from oraclous_knowledge_retriever_service.services.retrieval_service import RetrievalService

_bearer = HTTPBearer(auto_error=False)


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
    resolver = StaticMembershipResolver(uuid.UUID(get_settings().dev_org_id))
    context = await resolve_organisation_context(principal, resolver=resolver)
    with use_organisation_context(context):
        yield context


def get_current_user_id(principal: Annotated[Principal, Depends(get_principal)]) -> uuid.UUID:
    return principal.principal_id


def get_neo4j_driver(request: Request) -> Driver:
    driver = getattr(request.app.state, "neo4j_driver", None)
    if driver is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="knowledge graph store unavailable (KRS_NEO4J_URI not configured)",
        )
    return driver


def get_retrieval_service(
    driver: Annotated[Driver, Depends(get_neo4j_driver)],
    _org: Annotated[OrganisationContext, Depends(bind_org_context)],
) -> RetrievalService:
    settings = get_settings()
    return RetrievalService(
        driver,
        HashingEmbedder(dim=settings.embedding_dim),
        fulltext_index=settings.chunk_fulltext_index,
        database=settings.neo4j_database,
    )


UserIdDep = Annotated[uuid.UUID, Depends(get_current_user_id)]
RetrievalServiceDep = Annotated[RetrievalService, Depends(get_retrieval_service)]
