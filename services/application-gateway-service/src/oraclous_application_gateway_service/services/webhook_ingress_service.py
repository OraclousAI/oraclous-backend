"""Webhook ingress (services layer) — verify an inbound signed webhook, then fire an
engine event under the subscription's org.

The inbound POST carries NO bearer; the subscription id + the HMAC signature ARE the credential. The
whole auth-failure family (unknown/disabled subscription, unresolvable secret, bad signature,
an unpublished target) raises ``SubscriptionNotFound`` -> a uniform 404 so the id space can't be
enumerated. On success the gateway mints a SERVICE_ACCOUNT principal from the subscription's org and
fires POST /v1/engine/events with the ADR-018 trusted headers — the external caller asserts nothing.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Mapping

from oraclous_governance import Principal, PrincipalType

from oraclous_application_gateway_service.domain.webhook_signature import verify_signature
from oraclous_application_gateway_service.repositories.published_agent_repository import (
    PublishedAgentRepository,
)
from oraclous_application_gateway_service.repositories.rate_limit_store import enforce_bucket
from oraclous_application_gateway_service.repositories.upstream_client import UpstreamClient
from oraclous_application_gateway_service.repositories.webhook_subscription_repository import (
    WebhookSubscriptionRepository,
)
from oraclous_application_gateway_service.services.proxy_service import forward_request_headers
from oraclous_application_gateway_service.services.webhook_secret_client import (
    BrokerSecretError,
    WebhookSecretClient,
)

_MAX_INPUT_CHARS = 8000  # bound the event payload folded into the agent goal


class SubscriptionNotFound(Exception):
    """The whole inbound auth-failure family -> a uniform 404 (anti-enumeration)."""


class UpstreamEngineError(Exception):
    """The engine event-fire could not be reached / returned non-2xx (-> 502)."""


class WebhookRateLimited(Exception):
    """The subscription exceeded its per-subscription rate limit (-> 429 + Retry-After, S3)."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("webhook subscription rate limit exceeded")
        self.retry_after = retry_after


_WH_RL_NS = "rl:websub:"  # the per-subscription rate-limit bucket namespace


def _build_input(raw_body: bytes) -> str:
    payload = raw_body.decode("utf-8", errors="replace")[:_MAX_INPUT_CHARS]
    return f"A webhook event was received. Payload:\n{payload}"


class WebhookIngressService:
    def __init__(
        self,
        *,
        subscriptions: WebhookSubscriptionRepository,
        agents: PublishedAgentRepository,
        secret_client: WebhookSecretClient,
        upstream_client: UpstreamClient,
        engine_base_url: str,
        internal_key: str,
        redis=None,  # noqa: ANN001 — redis.asyncio client | None (per-subscription limit, S3)
        rate_limit: int = 600,
        rate_window_seconds: int = 60,
        allow_during_outage: bool = True,
    ) -> None:
        self._subs = subscriptions
        self._agents = agents
        self._secrets = secret_client
        self._upstream = upstream_client
        self._base_url = engine_base_url.rstrip("/")
        self._internal_key = internal_key
        self._redis = redis
        self._rate_limit = rate_limit
        self._rate_window = rate_window_seconds
        # Redis-outage policy for the per-subscription limit (ADR-021 §1): True = fail-open (allow +
        # alert), False = fail-closed (enforce_bucket raises RateLimiterUnavailable -> route 503s).
        self._allow_during_outage = allow_during_outage

    async def ingest(
        self,
        *,
        subscription_id: uuid.UUID,
        raw_body: bytes,
        headers: Mapping[str, str],
        delivery_id: str | None,
    ) -> None:
        sub = await self._subs.get_by_id(subscription_id)
        if sub is None or not sub.enabled:
            raise SubscriptionNotFound()
        # per-subscription rate limit (R7-SEC S3): one abused subscription is throttled
        # independently of the per-IP edge floor, BEFORE the (more expensive) broker resolve +
        # HMAC verify. Fail-open (a missing/erroring Redis allows it, like the edge limiter).
        decision = await enforce_bucket(
            self._redis,
            identity=str(sub.id),
            limit=self._rate_limit,
            window_seconds=self._rate_window,
            namespace=_WH_RL_NS,
            allow_during_outage=self._allow_during_outage,
        )
        if not decision.allowed:
            raise WebhookRateLimited(decision.retry_after)
        # resolve the signing secret from the broker (never stored here); unresolvable -> reject
        try:
            secret = await self._secrets.resolve(
                organisation_id=sub.organisation_id, secret_id=sub.broker_secret_ref
            )
        except BrokerSecretError as exc:
            raise SubscriptionNotFound() from exc
        if secret is None:
            raise SubscriptionNotFound()
        # verify the signature over the EXACT raw bytes under the sub's PINNED scheme; timestamped
        # schemes also enforce a replay window. Bad/absent -> 404 (uniform anti-enumeration).
        if not verify_signature(
            sub.signature_scheme,
            secret=secret,
            raw_body=raw_body,
            headers=headers,
            now_unix=int(time.time()),
        ):
            raise SubscriptionNotFound()
        # the bound published agent must still exist + be active (fail-closed if unpublished)
        agent = await self._agents.get_by_slug(
            organisation_id=sub.organisation_id, slug=sub.target_slug
        )
        if agent is None or agent.status != "active":
            raise SubscriptionNotFound()
        await self._fire_event(sub=sub, agent=agent, raw_body=raw_body, delivery_id=delivery_id)

    async def _fire_event(self, *, sub, agent, raw_body: bytes, delivery_id: str | None) -> None:  # noqa: ANN001
        # the gateway attests org + a service principal; the external caller set no trust headers
        principal = Principal(
            principal_id=sub.id,
            principal_type=PrincipalType.SERVICE_ACCOUNT,
            organisation_id=sub.organisation_id,
        )
        idempotency_key = delivery_id or hashlib.sha256(raw_body).hexdigest()
        body = json.dumps(
            {
                "manifest_ref": agent.bound_capability_ref,
                "input": _build_input(raw_body),
                "idempotency_key": idempotency_key,
                "event_type": "webhook",
                "source": str(sub.id),
            }
        ).encode()
        headers = forward_request_headers(
            [(b"content-type", b"application/json")], principal, internal_key=self._internal_key
        )
        resp = await self._upstream.open(
            method="POST",
            url=f"{self._base_url}/v1/engine/events",
            headers=headers,
            params=None,
            content=body,
        )
        try:
            code = resp.status_code
            await resp.aread()
        finally:
            await resp.aclose()
        if code not in (200, 201, 202):
            raise UpstreamEngineError(f"engine returned {code}")
