"""Webhook-subscription management (ORAA-4 §21 services layer) — member self-service CRUD.

A member registers a webhook for one of their org's PUBLISHED agents: the service mints a signing
secret, stores it in the broker (only the reference lands in the gateway), and returns the plaintext
ONCE (the member configures it on their webhook source — it is never retrievable again). List/delete
are org-scoped (ADR-006). Binding to a published-agent slug means unpublishing it disables the
webhook — one coherent management surface.
"""

from __future__ import annotations

import secrets
import uuid

from oraclous_application_gateway_service.models.webhook_subscription import WebhookSubscription
from oraclous_application_gateway_service.repositories.published_agent_repository import (
    PublishedAgentRepository,
)
from oraclous_application_gateway_service.repositories.webhook_subscription_repository import (
    WebhookSubscriptionRepository,
)
from oraclous_application_gateway_service.services.webhook_secret_client import WebhookSecretClient


class UnknownAgent(Exception):
    """No active published agent at this slug in the member's org (-> 404)."""


class WebhookSubscriptionService:
    def __init__(
        self,
        *,
        subscriptions: WebhookSubscriptionRepository,
        agents: PublishedAgentRepository,
        secret_client: WebhookSecretClient,
    ) -> None:
        self._subs = subscriptions
        self._agents = agents
        self._secrets = secret_client

    async def create(
        self, *, organisation_id: uuid.UUID, agent_slug: str
    ) -> tuple[WebhookSubscription, str]:
        agent = await self._agents.get_by_slug(organisation_id=organisation_id, slug=agent_slug)
        if agent is None or agent.status != "active":
            raise UnknownAgent(agent_slug)
        signing_secret = "whsec_" + secrets.token_urlsafe(32)
        secret_ref = await self._secrets.mint(
            organisation_id=organisation_id, secret=signing_secret
        )
        sub = await self._subs.create(
            organisation_id=organisation_id, target_slug=agent_slug, broker_secret_ref=secret_ref
        )
        return sub, signing_secret  # the plaintext returned ONCE

    async def list_subscriptions(self, *, organisation_id: uuid.UUID) -> list[WebhookSubscription]:
        return await self._subs.list_for_org(organisation_id)

    async def delete(self, *, organisation_id: uuid.UUID, subscription_id: uuid.UUID) -> bool:
        return await self._subs.delete_for_org(
            subscription_id=subscription_id, organisation_id=organisation_id
        )
