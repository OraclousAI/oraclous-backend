"""App lifecycle (ORAA-4 §21 core layer) — open/close the upstream HTTP client, Redis, and the
gateway DB.

The gateway's substrate is the upstream services (one shared ``httpx.AsyncClient``), a Redis
connection used only by the edge rate limiter (Slice 2), and — since Slice 3 (ADR-019) — its own
Postgres holding the integration-key / published-agent / chat / webhook-subscription stores. Both
Redis and the DB are opened **graceful-degrade**: if either cannot be created, its ``app.state``
slot is ``None`` so ``/health`` stays up; the rate limiter then fails open, and the DB-backed paths
return 503 (never a crash).

ADR-030 §3 (RLS backstop): the gateway carves its DB access into TWO engines. The ORG-BOUND repos
(integration-key / published-agent / chat / webhook-subscription) connect on ``DATABASE_URL`` as the
NOSUPERUSER ``oraclous_app`` role with the org-GUC guard installed (``build_rls_engine`` via the
repos' ``install_guard=True`` default), so RLS bites + each org-bound op binds the GUC via
``org_scope``. The TWO pre-auth PRODUCER reads — integration-key ``get_by_prefix`` and
webhook-subscription ``get_by_id`` — resolve an org/credential BEFORE any org context, so they run
on SEPARATE OWNER-engine repos (``owner_database_url`` + ``install_guard=False``) that bypass RLS;
else FORCE'd RLS fails them closed and breaks integration-key auth + inbound webhooks (the HARD
RULE). The two owner-engine repos serve ONLY those producer reads; everything else is org-bound. The
gateway
runs no Celery/background worker that touches these tables (webhook ingress is synchronous — it
proxies inbound to the engine over HTTP), so the web lifespan startup assertion is the sole
non-bypassing-role chokepoint.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI
from oraclous_telemetry import Severity, alert

from oraclous_application_gateway_service.core.config import get_settings
from oraclous_application_gateway_service.core.rls import (
    RlsBypassingRoleError,
    assert_runtime_role_isolates,
    build_rls_engine,
)
from oraclous_application_gateway_service.domain.route_table import build_route_table
from oraclous_application_gateway_service.repositories.chat_repository import ChatRepository
from oraclous_application_gateway_service.repositories.integration_key_repository import (
    IntegrationKeyRepository,
)
from oraclous_application_gateway_service.repositories.published_agent_repository import (
    PublishedAgentRepository,
)
from oraclous_application_gateway_service.repositories.upstream_client import UpstreamClient
from oraclous_application_gateway_service.repositories.webhook_subscription_repository import (
    WebhookSubscriptionRepository,
)
from oraclous_application_gateway_service.services.proxy_service import ProxyService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # ADR-030 §3: fail closed LOUDLY if the ORG-BOUND runtime role bypasses RLS (a superuser /
    # BYPASSRLS role makes the FORCE'd policy inert — T1-M3). A mis-deployed bypassing role is a
    # hard configuration error, so it exits the process rather than quietly serving an unscoped
    # store.
    # Gated on GATEWAY_RLS_ASSERT_RUNTIME_ROLE (the deployed oraclous_app api sets it; a deliberate
    # owner-DSN dev/test run leaves it off). Asserts the org-bound DSN the request path uses — the
    # owner engine that serves the two pre-auth producer reads is intended to bypass RLS and is not
    # asserted.
    if settings.GATEWAY_RLS_ASSERT_RUNTIME_ROLE:
        assert_engine = build_rls_engine(settings.DATABASE_URL)
        try:
            await assert_runtime_role_isolates(assert_engine)
        except RlsBypassingRoleError as exc:
            alert(
                Severity.ERROR,
                "rls_runtime_role_bypasses",
                "application-gateway-service",
                "runtime DB role bypasses RLS; refusing to start (ADR-030 §3)",
                error=str(exc),
            )
            await assert_engine.dispose()
            raise SystemExit(1) from exc
        await assert_engine.dispose()

    timeout = httpx.Timeout(
        connect=settings.UPSTREAM_CONNECT_TIMEOUT,
        read=settings.UPSTREAM_READ_TIMEOUT,
        write=settings.UPSTREAM_READ_TIMEOUT,
        pool=settings.UPSTREAM_CONNECT_TIMEOUT,
    )
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    app.state.http_client = client
    app.state.route_table = build_route_table(settings)
    app.state.proxy_service = ProxyService(
        route_table=app.state.route_table,
        upstream_client=UpstreamClient(client),
        internal_key=settings.INTERNAL_SERVICE_KEY,
    )
    try:
        # short socket timeouts: from_url is lazy (connect-on-use), so a partitioned Redis must time
        # out fast on the first command, not block the edge for the OS default connect timeout.
        app.state.redis = aioredis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open: a Redis outage must not lock the edge
        logger.warning("gateway: Redis unavailable (%s); the edge rate limiter will fail open", exc)
        app.state.redis = None
    try:
        # graceful-degrade: a DB problem leaves /health up + the DB-backed routes return 503.
        # ORG-BOUND repos on the oraclous_app DSN (the org-GUC guard installed by default).
        app.state.integration_key_repo = IntegrationKeyRepository(settings.DATABASE_URL)
        app.state.published_agent_repo = PublishedAgentRepository(settings.DATABASE_URL)
        app.state.chat_repo = ChatRepository(settings.DATABASE_URL)
        app.state.webhook_subscription_repo = WebhookSubscriptionRepository(settings.DATABASE_URL)
        # OWNER-engine repos for the two pre-auth producer reads ONLY (get_by_prefix / get_by_id):
        # the owner DSN bypasses RLS, guard off (ADR-030 §3). Defaults to DATABASE_URL when no split
        # DSN is set (single-DSN dev/test — both are the owner and RLS is a no-op).
        app.state.integration_key_owner_repo = IntegrationKeyRepository(
            settings.owner_database_url, install_guard=False
        )
        app.state.webhook_subscription_owner_repo = WebhookSubscriptionRepository(
            settings.owner_database_url, install_guard=False
        )
    except Exception as exc:  # noqa: BLE001 — never crash the edge on a DB issue
        logger.warning("gateway: datastore unavailable (%s); the DB-backed routes return 503", exc)
        app.state.integration_key_repo = None
        app.state.published_agent_repo = None
        app.state.chat_repo = None
        app.state.webhook_subscription_repo = None
        app.state.integration_key_owner_repo = None
        app.state.webhook_subscription_owner_repo = None
    try:
        yield
    finally:
        await client.aclose()
        if app.state.redis is not None:
            await app.state.redis.aclose()
        if app.state.integration_key_repo is not None:
            await app.state.integration_key_repo.close()
        if app.state.published_agent_repo is not None:
            await app.state.published_agent_repo.close()
        if app.state.chat_repo is not None:
            await app.state.chat_repo.close()
        if app.state.webhook_subscription_repo is not None:
            await app.state.webhook_subscription_repo.close()
        if getattr(app.state, "integration_key_owner_repo", None) is not None:
            await app.state.integration_key_owner_repo.close()
        if getattr(app.state, "webhook_subscription_owner_repo", None) is not None:
            await app.state.webhook_subscription_owner_repo.close()
