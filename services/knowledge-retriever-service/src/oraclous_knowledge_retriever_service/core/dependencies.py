"""DI providers (ORAA-4 §21 core layer) — wiring only.

`bind_org_context` resolves the caller's organisation from the authenticated principal and binds it
into the governance ContextVar for the request; every repository read then sees it via
`enforced_organisation_id()` (fail-closed). Exposed as `Annotated[...]` aliases (B008-clean)
route signatures.
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

from oraclous_knowledge_retriever_service.core.auth import (
    AuthError,
    StaticMembershipResolver,
    principal_from_gateway_headers,
    verify_token,
)
from oraclous_knowledge_retriever_service.core.config import get_settings
from oraclous_knowledge_retriever_service.services.embedder import HashingEmbedder
from oraclous_knowledge_retriever_service.services.eval_judge import EvalJudge, make_judge
from oraclous_knowledge_retriever_service.services.evaluation_service import EvaluationService
from oraclous_knowledge_retriever_service.services.retrieval_service import RetrievalService

_bearer = HTTPBearer(auto_error=False)


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


def get_redis_client(request: Request):
    """The advisory query-cache Redis client (None when the cache is disabled/unbound, #308)."""
    return getattr(request.app.state, "redis_client", None)


def get_retrieval_service(
    request: Request,
    driver: Annotated[Driver, Depends(get_neo4j_driver)],
    _org: Annotated[OrganisationContext, Depends(bind_org_context)],
) -> RetrievalService:
    settings = get_settings()
    return RetrievalService(
        driver,
        HashingEmbedder(dim=settings.embedding_dim),
        database=settings.neo4j_database,
        redis_client=get_redis_client(request),
        cache_ttl=settings.query_cache_ttl,
    )


def get_eval_judge() -> EvalJudge:
    """The LLM judge behind /evaluate (#331), or a typed 422 when no key is configured.

    An explicit evaluation endpoint must refuse rather than silently fabricate scores, so a
    missing KRS_OPENAI_API_KEY is a caller-visible, machine-readable 422 — never fake numbers.
    """
    judge = make_judge(get_settings())
    if judge is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "eval_judge_not_configured",
                "message": (
                    "evaluation requires an LLM judge: set KRS_OPENAI_API_KEY (and optionally "
                    "KRS_OPENAI_BASE_URL / KRS_EVAL_JUDGE_MODEL)."
                ),
            },
        )
    return judge


def get_evaluation_service(
    retrieval: Annotated[RetrievalService, Depends(get_retrieval_service)],
    judge: Annotated[EvalJudge, Depends(get_eval_judge)],
) -> EvaluationService:
    settings = get_settings()
    return EvaluationService(
        retrieval=retrieval,
        judge=judge,
        top_k=settings.eval_top_k,
        max_concurrency=settings.eval_max_concurrency,
        max_claims=settings.eval_max_claims,
        max_contexts=settings.eval_max_contexts,
        grounded_threshold=settings.eval_grounded_threshold,
    )


UserIdDep = Annotated[uuid.UUID, Depends(get_current_user_id)]
RetrievalServiceDep = Annotated[RetrievalService, Depends(get_retrieval_service)]
EvaluationServiceDep = Annotated[EvaluationService, Depends(get_evaluation_service)]
