"""DI providers (ORAA-4 §21 core layer) — wiring only.

The shared upstream HTTP client, the route table, and the proxy service are opened/built in
``core/lifespan`` and resolved per request from ``app.state``.
"""

from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Query, Request, status
from oraclous_governance import Principal, PrincipalType, org_role_at_least
from sqlalchemy.exc import SQLAlchemyError

from oraclous_application_gateway_service.core.auth import AuthError, verify_token
from oraclous_application_gateway_service.core.config import get_settings
from oraclous_application_gateway_service.domain.auth_policy import is_public
from oraclous_application_gateway_service.domain.integration_key import is_integration_key
from oraclous_application_gateway_service.domain.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Pagination,
)
from oraclous_application_gateway_service.domain.upstreams import upstream_health_targets
from oraclous_application_gateway_service.repositories.chat_repository import ChatRepository
from oraclous_application_gateway_service.repositories.integration_key_repository import (
    IntegrationKeyRepository,
)
from oraclous_application_gateway_service.repositories.published_agent_repository import (
    PublishedAgentRepository,
)
from oraclous_application_gateway_service.repositories.rate_limit_store import (
    RateLimiterUnavailable,
    enforce_bucket,
)
from oraclous_application_gateway_service.repositories.upstream_client import UpstreamClient
from oraclous_application_gateway_service.repositories.webhook_subscription_repository import (
    WebhookSubscriptionRepository,
)
from oraclous_application_gateway_service.services.chat_service import ChatService
from oraclous_application_gateway_service.services.chat_turn_service import ChatTurnService
from oraclous_application_gateway_service.services.health_service import HealthService
from oraclous_application_gateway_service.services.integration_key_auth_service import (
    IntegrationKeyAuthService,
    ResolvedKey,
)
from oraclous_application_gateway_service.services.integration_key_management_service import (
    IntegrationKeyManagementService,
)
from oraclous_application_gateway_service.services.invoke_service import InvokeService
from oraclous_application_gateway_service.services.mcp_service import McpService
from oraclous_application_gateway_service.services.proxy_service import ProxyService
from oraclous_application_gateway_service.services.published_agent_service import (
    PublishedAgentService,
)
from oraclous_application_gateway_service.services.webhook_ingress_service import (
    WebhookIngressService,
)
from oraclous_application_gateway_service.services.webhook_secret_client import WebhookSecretClient
from oraclous_application_gateway_service.services.webhook_subscription_service import (
    WebhookSubscriptionService,
)

_IK_RL_NS = "rl:ik:key:"  # the per-key rate-limit bucket namespace (R7-SEC S3)


def pagination_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Pagination:
    """OPTIONAL ``limit``/``offset`` for every collection read (WP-10). Both default
    backward-compatibly (a caller that passes neither gets the first ``DEFAULT_LIMIT`` rows, the
    prior behaviour for any realistic page); ``limit`` is bounded ``[1, MAX_LIMIT]`` by FastAPI's
    validation so no single read is unbounded. The response shape is unchanged — a plain list."""
    return Pagination(limit=limit, offset=offset)


def get_http_client(request: Request) -> httpx.AsyncClient:
    client = getattr(request.app.state, "http_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="gateway HTTP client unavailable",
        )
    return client


def get_proxy_service(request: Request) -> ProxyService:
    svc = getattr(request.app.state, "proxy_service", None)
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="gateway proxy unavailable",
        )
    return svc


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


async def get_edge_principal(request: Request) -> Principal | None:
    """Terminate identity at the edge: ``None`` for public allow-list paths, else a verified
    Principal (401 on missing/invalid/expired token — fail-closed before any upstream call). An
    ``oak-``/``oag-`` bearer is an integration key validated against the gateway store (Slice 3);
    any other bearer is a JWT (dev/jwt mode)."""
    if is_public(request.url.path):
        return None
    token = _bearer_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    principal = await _authenticate(request, token)
    # belt-and-braces: an authenticated principal MUST carry an org, else the proxy's
    # strip-then-assert omits X-Organisation-Id (the client copy is already stripped), forwarding
    # a tenant-unscoped authenticated request. Fail closed, don't rely solely on the DB constraint.
    if principal.organisation_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token is missing organisation_id",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


async def _authenticate(request: Request, token: str) -> Principal:
    if is_integration_key(token):
        key_repo = getattr(request.app.state, "integration_key_repo", None)
        if key_repo is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="integration-key store unavailable",
            )
        try:
            resolved = await IntegrationKeyAuthService(key_repo).resolve(token)
        except AuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        except (SQLAlchemyError, OSError) as exc:
            # the DB dropped mid-flight -> the key path degrades to 503 (not a 500/crash)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="integration-key store unavailable",
            ) from exc
        # carry the binding so the invoke route can enforce it pre-forward (S4 PR2)
        request.state.resolved_key = resolved
        # per-key rate limit (R7-SEC S3): a key with a configured cap is throttled independently of
        # the edge per-IP window. On a Redis outage the configured policy applies (ADR-021 §1):
        # default fail-open (allow + alert); opt-in fail-closed -> 503.
        if resolved.rate_limit is not None:
            try:
                decision = await enforce_bucket(
                    getattr(request.app.state, "redis", None),
                    identity=str(resolved.key_id),
                    limit=resolved.rate_limit,
                    window_seconds=resolved.rate_window_seconds or 60,
                    namespace=_IK_RL_NS,
                    allow_during_outage=get_settings().RATE_LIMIT_ALLOW_DURING_OUTAGE,
                )
            except RateLimiterUnavailable as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="rate limiter unavailable (fail-closed)",
                ) from exc
            if not decision.allowed:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="integration key rate limit exceeded",
                    headers={"Retry-After": str(decision.retry_after)},
                )
        return resolved.principal
    try:
        return verify_token(token)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def get_health_service(request: Request) -> HealthService:
    client = get_http_client(request)
    return HealthService(
        upstream_client=UpstreamClient(client),
        targets=upstream_health_targets(get_settings()),
    )


def _require_repo(request: Request, attr: str):  # noqa: ANN202 — returns the repo or 503s
    repo = getattr(request.app.state, attr, None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="gateway datastore unavailable",
        )
    return repo


def get_integration_key_repository(request: Request) -> IntegrationKeyRepository:
    return _require_repo(request, "integration_key_repo")


def get_published_agent_repository(request: Request) -> PublishedAgentRepository:
    return _require_repo(request, "published_agent_repo")


async def require_member(principal: EdgePrincipalDep) -> Principal:
    """A management route requires an authenticated MEMBER (a user JWT) — never an integration key
    (a key cannot manage keys). Org-scoping (a member manages only their own org) is applied by the
    caller via ``principal.organisation_id``. READ-level management (list/get) is member; the
    DESTRUCTIVE ops require ``require_admin`` (R7-SEC S2, the org-roles floor)."""
    if principal is None or principal.principal_type != PrincipalType.USER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this operation requires a member credential",
        )
    return principal


async def require_admin(member: MemberDep) -> Principal:
    """A DESTRUCTIVE op (mint/rotate/revoke a key, publish an agent, create/delete a webhook
    subscription) requires an org ADMIN (owner ≥ admin). Builds on ``require_member`` (a user,
    key), then asserts the verified ``org_role`` ranks at least admin — fail-closed: a None / member
    role is 403. The role is auth-issued (the JWT ``org_role`` claim), never client-set."""
    if not org_role_at_least(member.org_role, minimum="admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this operation requires an organisation admin",
        )
    return member


async def require_bound_key(request: Request, principal: EdgePrincipalDep) -> ResolvedKey:
    """The public published-agent surface requires an INTEGRATION KEY (never a member JWT) whose
    binding matches the invoked agent. ``get_edge_principal`` already resolved + stashed the key on
    request.state; here we assert it is a key (SERVICE_ACCOUNT) and that its ``bound_agent_slug``
    equals the path ``slug`` — fail-closed 403 otherwise, before any upstream call. A cap-only
    key (no bound slug) never matches a published-agent slug, so it is rejected too."""
    resolved = getattr(request.state, "resolved_key", None)
    if (
        resolved is None
        or principal is None
        or principal.principal_type != PrincipalType.SERVICE_ACCOUNT
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this endpoint requires an integration key",
        )
    if resolved.bound_agent_slug is None or resolved.bound_agent_slug != request.path_params.get(
        "slug"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this integration key is not bound to that agent",
        )
    return resolved


async def require_mcp_key(request: Request, principal: EdgePrincipalDep) -> ResolvedKey:
    """The MCP server plane requires an INTEGRATION KEY (a programmatic client), never a member JWT.
    ``get_edge_principal`` already resolved + stashed the key; assert it is a key (SERVICE_ACCOUNT —
    a member JWT is 403) and return the ``ResolvedKey``. Unlike ``require_bound_key`` there is no
    single path slug to match — the per-tool binding scopes ``tools/list`` + ``tools/call`` in the
    service (a key with no binding sees + calls nothing, fail-closed)."""
    resolved = getattr(request.state, "resolved_key", None)
    if (
        resolved is None
        or principal is None
        or principal.principal_type != PrincipalType.SERVICE_ACCOUNT
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the MCP endpoint requires an integration key",
        )
    return resolved


def get_mcp_service(request: Request, agents: PublishedAgentRepoDep) -> McpService:
    return McpService(agents=agents, invoke=get_invoke_service(request, agents))


def get_key_management_service(
    keys: IntegrationKeyRepoDep, agents: PublishedAgentRepoDep
) -> IntegrationKeyManagementService:
    return IntegrationKeyManagementService(keys=keys, agents=agents)


def get_published_agent_service(agents: PublishedAgentRepoDep) -> PublishedAgentService:
    return PublishedAgentService(agents)


def get_invoke_service(request: Request, agents: PublishedAgentRepoDep) -> InvokeService:
    settings = get_settings()
    return InvokeService(
        agents=agents,
        upstream_client=UpstreamClient(get_http_client(request)),
        harness_base_url=settings.HARNESS_RUNTIME_URL,
        internal_key=settings.INTERNAL_SERVICE_KEY,
    )


def get_chat_repository(request: Request) -> ChatRepository:
    return _require_repo(request, "chat_repo")


def get_chat_service(chats: ChatRepoDep, agents: PublishedAgentRepoDep) -> ChatService:
    return ChatService(threads=chats, agents=agents)


def get_chat_turn_service(
    request: Request, chats: ChatRepoDep, agents: PublishedAgentRepoDep
) -> ChatTurnService:
    settings = get_settings()
    return ChatTurnService(
        threads=chats,
        agents=agents,
        upstream_client=UpstreamClient(get_http_client(request)),
        harness_base_url=settings.HARNESS_RUNTIME_URL,
        internal_key=settings.INTERNAL_SERVICE_KEY,
    )


def get_webhook_subscription_repository(request: Request) -> WebhookSubscriptionRepository:
    return _require_repo(request, "webhook_subscription_repo")


def _webhook_secret_client(request: Request) -> WebhookSecretClient:
    settings = get_settings()
    return WebhookSecretClient(
        upstream_client=UpstreamClient(get_http_client(request)),
        broker_base_url=settings.CREDENTIAL_BROKER_URL,
        internal_key=settings.INTERNAL_SERVICE_KEY,
    )


def get_webhook_subscription_service(
    request: Request, subs: WebhookSubscriptionRepoDep, agents: PublishedAgentRepoDep
) -> WebhookSubscriptionService:
    return WebhookSubscriptionService(
        subscriptions=subs, agents=agents, secret_client=_webhook_secret_client(request)
    )


def get_webhook_ingress_service(
    request: Request, subs: WebhookSubscriptionRepoDep, agents: PublishedAgentRepoDep
) -> WebhookIngressService:
    settings = get_settings()
    return WebhookIngressService(
        subscriptions=subs,
        agents=agents,
        secret_client=_webhook_secret_client(request),
        upstream_client=UpstreamClient(get_http_client(request)),
        engine_base_url=settings.EXECUTION_ENGINE_URL,
        internal_key=settings.INTERNAL_SERVICE_KEY,
        redis=getattr(request.app.state, "redis", None),
        rate_limit=settings.WEBHOOK_RATE_LIMIT,
        rate_window_seconds=settings.WEBHOOK_RATE_WINDOW_SECONDS,
        allow_during_outage=settings.RATE_LIMIT_ALLOW_DURING_OUTAGE,
    )


HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]
ProxyServiceDep = Annotated[ProxyService, Depends(get_proxy_service)]
EdgePrincipalDep = Annotated[Principal | None, Depends(get_edge_principal)]
HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]
IntegrationKeyRepoDep = Annotated[IntegrationKeyRepository, Depends(get_integration_key_repository)]
PublishedAgentRepoDep = Annotated[PublishedAgentRepository, Depends(get_published_agent_repository)]
MemberDep = Annotated[Principal, Depends(require_member)]
AdminDep = Annotated[Principal, Depends(require_admin)]
BoundKeyDep = Annotated[ResolvedKey, Depends(require_bound_key)]
KeyManagementDep = Annotated[IntegrationKeyManagementService, Depends(get_key_management_service)]
PublishedAgentServiceDep = Annotated[PublishedAgentService, Depends(get_published_agent_service)]
InvokeServiceDep = Annotated[InvokeService, Depends(get_invoke_service)]
ChatRepoDep = Annotated[ChatRepository, Depends(get_chat_repository)]
ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]
ChatTurnServiceDep = Annotated[ChatTurnService, Depends(get_chat_turn_service)]
WebhookSubscriptionRepoDep = Annotated[
    WebhookSubscriptionRepository, Depends(get_webhook_subscription_repository)
]
WebhookSubscriptionServiceDep = Annotated[
    WebhookSubscriptionService, Depends(get_webhook_subscription_service)
]
WebhookIngressServiceDep = Annotated[WebhookIngressService, Depends(get_webhook_ingress_service)]
McpKeyDep = Annotated[ResolvedKey, Depends(require_mcp_key)]
McpServiceDep = Annotated[McpService, Depends(get_mcp_service)]
PaginationDep = Annotated[Pagination, Depends(pagination_params)]
