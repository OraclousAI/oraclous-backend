"""Webhook-subscription management (services layer) — member self-service CRUD.

A member registers a webhook for one of their org's PUBLISHED agents: the service mints a signing
secret, stores it in the broker (only the reference lands in the gateway), and returns the plaintext
ONCE (the member configures it on their webhook source — it is never retrievable again). List/delete
are org-scoped (ADR-006). Binding to a published-agent slug means unpublishing it disables the
webhook — one coherent management surface.
"""

from __future__ import annotations

import secrets
import uuid

from oraclous_application_gateway_service.domain.pagination import DEFAULT_LIMIT
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
        owner_subscriptions: WebhookSubscriptionRepository | None = None,
    ) -> None:
        self._subs = subscriptions
        self._agents = agents
        self._secrets = secret_client
        # ``get_by_id`` is the pre-auth producer read and lives on the OWNER engine (it is NOT
        # org-bound; on the org-bound engine RLS would fail it closed to zero rows — ADR-030 §3).
        # ``delete`` uses it to read the sub (to GC its broker secret) before the org-scoped
        # ``delete_for_org`` on the org-bound engine; the ``sub.organisation_id != organisation_id``
        # check is the tenant guard. Defaults to the org-bound repo for back-compat in tests that
        # pass a single repo (a single-engine test has no RLS, so get_by_id resolves either way).
        self._owner_subs = owner_subscriptions or subscriptions

    async def create(
        self, *, organisation_id: uuid.UUID, agent_slug: str, signature_scheme: str = "generic"
    ) -> tuple[WebhookSubscription, str]:
        agent = await self._agents.get_by_slug(organisation_id=organisation_id, slug=agent_slug)
        if agent is None or agent.status != "active":
            raise UnknownAgent(agent_slug)
        signing_secret = "whsec_" + secrets.token_urlsafe(32)
        secret_ref = await self._secrets.mint(
            organisation_id=organisation_id, secret=signing_secret
        )
        try:
            sub = await self._subs.create(
                organisation_id=organisation_id,
                target_slug=agent_slug,
                broker_secret_ref=secret_ref,
                signature_scheme=signature_scheme,
            )
        except Exception:
            # the sub-row insert failed AFTER the broker minted the secret -> compensate so it
            # isn't orphaned (R7-SEC S4). Best-effort: delete never raises; re-raise the real error.
            await self._secrets.delete(organisation_id=organisation_id, secret_id=secret_ref)
            raise
        return sub, signing_secret  # the plaintext returned ONCE

    async def list_subscriptions(
        self,
        *,
        organisation_id: uuid.UUID,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[WebhookSubscription]:
        return await self._subs.list_for_org(organisation_id, limit=limit, offset=offset)

    async def delete(self, *, organisation_id: uuid.UUID, subscription_id: uuid.UUID) -> bool:
        # read the sub first (on the OWNER engine — get_by_id is the pre-auth producer read; the
        # org-bound engine would fail it closed under RLS) so we can GC its broker secret; deleting
        # the row alone orphans the secret in the broker (R7-SEC S4). The org check below is the
        # tenant guard (a cross-tenant sub is rejected before the org-bound delete).
        sub = await self._owner_subs.get_by_id(subscription_id)
        if sub is None or sub.organisation_id != organisation_id:
            return False
        deleted = await self._subs.delete_for_org(
            subscription_id=subscription_id, organisation_id=organisation_id
        )
        if deleted:
            # best-effort GC of the now-unreferenced secret (a failure leaves a harmless orphan)
            await self._secrets.delete(
                organisation_id=organisation_id, secret_id=sub.broker_secret_ref
            )
        return deleted
