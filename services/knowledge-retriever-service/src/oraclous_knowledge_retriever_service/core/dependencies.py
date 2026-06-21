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
from oraclous_eval import RubricEvaluator
from oraclous_governance import (
    OrganisationContext,
    Principal,
    resolve_organisation_context,
    use_organisation_context,
)
from oraclous_rebac import ReBACEngine
from oraclous_rebac.adapter import ReBACEngineResolver
from oraclous_substrate.rebac import AccessDecisionClient

from oraclous_knowledge_retriever_service.core.auth import (
    AuthError,
    StaticMembershipResolver,
    principal_from_gateway_headers,
    verify_token,
)
from oraclous_knowledge_retriever_service.core.config import Settings, get_settings
from oraclous_knowledge_retriever_service.services.embedder import HashingEmbedder
from oraclous_knowledge_retriever_service.services.eval_judge import EvalJudge
from oraclous_knowledge_retriever_service.services.evaluation_service import EvaluationService
from oraclous_knowledge_retriever_service.services.federated_service import (
    FederatedRetrievalService,
)
from oraclous_knowledge_retriever_service.services.flow_evaluation_service import (
    FlowEvaluationService,
)
from oraclous_knowledge_retriever_service.services.graph_registry_client import GraphRegistryClient
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


def get_federated_service(
    request: Request,
    driver: Annotated[Driver, Depends(get_neo4j_driver)],
    _org: Annotated[OrganisationContext, Depends(bind_org_context)],
) -> FederatedRetrievalService:
    """Build the federated cross-graph service (#330 / ADR-026). Fail-closed: with no
    KRS_KNOWLEDGE_GRAPH_URL (so no pooled registry client) the accessible set cannot be
    enumerated, so the federated surface 503s — it never falls back to "all graphs"."""
    settings = get_settings()
    fed_client = getattr(request.app.state, "federation_http_client", None)
    if not settings.knowledge_graph_url or fed_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="federation unavailable (KRS_KNOWLEDGE_GRAPH_URL not configured)",
        )
    registry = GraphRegistryClient(
        client=fed_client,
        auth_mode=settings.auth_mode,
        dev_bearer=settings.dev_bearer,
        internal_service_key=settings.internal_service_key,
    )
    # Cross-org ReBAC admission (#446): the fail-closed access-decision client that lets a caller
    # name a FOREIGN graph it has been granted (ADR-004). Wired only when the async Neo4j driver is
    # bound; None degrades admission to OFF (a foreign graph stays inaccessible — never fail-open).
    rebac_async_driver = getattr(request.app.state, "neo4j_async_driver", None)
    rebac_client = None
    if rebac_async_driver is not None:
        engine = ReBACEngine()
        rebac_client = AccessDecisionClient(
            resolver=ReBACEngineResolver(
                permission_check=engine.check_graph_permission,
                driver=rebac_async_driver,
                # ADR-036: the seam reads back the owner org of a granted graph so the fan-out can
                # bind it and return the owner's rows.
                owner_org_check=engine.grant_owner_org,
            )
        )
    return FederatedRetrievalService(
        driver,
        HashingEmbedder(dim=settings.embedding_dim),
        registry,
        database=settings.neo4j_database,
        max_graphs=settings.federated_max_graphs,
        max_per_graph_k=settings.federated_max_per_graph_k,
        max_total=settings.federated_max_total,
        max_subgraph_nodes=settings.federated_max_subgraph_nodes,
        rebac_client=rebac_client,
    )


def get_eval_judge(request: Request) -> EvalJudge:
    """The lifespan-built judge singleton behind /evaluate (#331), or a typed 422 when absent.

    An explicit evaluation endpoint must refuse rather than silently fabricate scores, so a
    missing KRS_OPENAI_API_KEY is a caller-visible, machine-readable 422 — never fake numbers.
    The detail uses the Pydantic LIST shape (``[{"loc": [...], "type": "...", "msg": "..."}]``)
    because the gateway's leak-safe #225 extractor relays ONLY that shape: the typed error
    survives the edge as VALIDATION_FAILED with field ``eval`` / issue
    ``EVAL_JUDGE_NOT_CONFIGURED`` instead of collapsing to the detail-free envelope (#333).
    """
    judge = getattr(request.app.state, "eval_judge", None)
    if judge is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[
                {
                    "loc": ["eval"],
                    "type": "eval_judge_not_configured",
                    "msg": (
                        "evaluation requires an LLM judge: set KRS_OPENAI_API_KEY (and optionally"
                        " KRS_OPENAI_BASE_URL / KRS_EVAL_JUDGE_MODEL)."
                    ),
                }
            ],
        )
    return judge


def get_eval_judge_optional(request: Request) -> EvalJudge | None:
    """The lifespan judge singleton, or ``None`` (no raise) — the BYOM-judge route can't hard-depend
    on the singleton (a request may bring its own per-org credential instead). The route maps a
    ``None`` singleton (and no BYOM credential) to the typed 422 via :func:`get_eval_judge`. Kept a
    DI provider (not a bare ``app.state`` read) so tests inject a fake judge via overrides."""
    judge: EvalJudge | None = getattr(request.app.state, "eval_judge", None)
    return judge


def get_evaluation_service(
    request: Request,
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
        deadline_seconds=settings.eval_deadline_seconds,
        # process-level evaluation slots (lifespan-built); absent (e.g. bare test app) → uncapped
        request_slots=getattr(request.app.state, "eval_slots", None),
    )


def build_flow_eval_service(
    judge: EvalJudge, request: Request, settings: Settings
) -> FlowEvaluationService:
    """Wrap an ``EvalJudge`` — the lifespan singleton OR a per-request BYOM judge (BYOM-judge) — in
    the shared ``RubricEvaluator`` → ``FlowEvaluationService``. ``eval_slots`` caps concurrent
    evaluations (→ 429) on BOTH paths; absent on a bare test app → uncapped."""
    evaluator = RubricEvaluator(
        judge,
        max_concurrency=settings.eval_max_concurrency,
        deadline_seconds=settings.eval_deadline_seconds,
        slots=getattr(request.app.state, "eval_slots", None),
    )
    return FlowEvaluationService(evaluator)


def get_flow_evaluation_service(
    request: Request,
    judge: Annotated[EvalJudge, Depends(get_eval_judge)],
    _org: Annotated[OrganisationContext, Depends(bind_org_context)],
) -> FlowEvaluationService:
    """core/evaluate (ADR-037 / #469): the shared packages/eval evaluator over the ONE lifespan
    judge (KRS's ``app.state.eval_judge`` duck-types ``oraclous_eval.EvalJudge``). Depends on
    ``bind_org_context`` so the request's org is bound before the route server-stamps it (H2); a
    missing judge surfaces as the typed 422 via ``get_eval_judge``. The BYOM-judge path (a per-org
    credential) is built in the route, not here, since it depends on the request body."""
    return build_flow_eval_service(judge, request, get_settings())


FlowEvalServiceDep = Annotated[FlowEvaluationService, Depends(get_flow_evaluation_service)]
UserIdDep = Annotated[uuid.UUID, Depends(get_current_user_id)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
RetrievalServiceDep = Annotated[RetrievalService, Depends(get_retrieval_service)]
FederatedServiceDep = Annotated[FederatedRetrievalService, Depends(get_federated_service)]
EvaluationServiceDep = Annotated[EvaluationService, Depends(get_evaluation_service)]
