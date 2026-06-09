"""Webhook ingress + subscription routes (ORAA-4 §21 routes layer).

Two planes:
* ``POST /v1/webhooks/{subscription_id}`` — PUBLIC (no bearer; the id + the HMAC signature are the
  credential). Verify over the raw body, fire an engine event. Auth-failure family -> 404 (uniform);
  the engine unreachable -> 502.
* ``/v1/webhook-subscriptions`` — MEMBER self-service CRUD (a USER JWT, org-scoped).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status

from oraclous_application_gateway_service.core.dependencies import (
    AdminDep,
    MemberDep,
    WebhookIngressServiceDep,
    WebhookSubscriptionServiceDep,
)
from oraclous_application_gateway_service.schema.webhook_schemas import (
    CreateSubscriptionRequest,
    CreateSubscriptionResponse,
    SubscriptionOut,
)
from oraclous_application_gateway_service.services.webhook_ingress_service import (
    SubscriptionNotFound,
    UpstreamEngineError,
    WebhookRateLimited,
)
from oraclous_application_gateway_service.services.webhook_subscription_service import UnknownAgent

router = APIRouter(tags=["webhooks"])

_SIG_HEADER = "x-hub-signature-256"  # generic scheme: sha256=<hmac-hex>
_DELIVERY_HEADER = "x-webhook-delivery"  # optional provider delivery id (else sha256(body) dedupes)


@router.post("/v1/webhooks/{subscription_id}", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(
    subscription_id: uuid.UUID, request: Request, service: WebhookIngressServiceDep
) -> Response:
    raw = await request.body()  # the SizeGuard buffered + replays the EXACT bytes (signature-safe)
    try:
        await service.ingest(
            subscription_id=subscription_id,
            raw_body=raw,
            signature_header=request.headers.get(_SIG_HEADER),
            delivery_id=request.headers.get(_DELIVERY_HEADER),
        )
    except SubscriptionNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found") from exc
    except WebhookRateLimited as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except UpstreamEngineError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="event dispatch failed"
        ) from exc
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/v1/webhook-subscriptions",
    response_model=CreateSubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_subscription(
    body: CreateSubscriptionRequest, admin: AdminDep, service: WebhookSubscriptionServiceDep
) -> CreateSubscriptionResponse:
    """Register a webhook for an org published agent. The signing secret is shown ONCE."""
    try:
        sub, secret = await service.create(
            organisation_id=admin.organisation_id, agent_slug=body.agent_slug
        )
    except UnknownAgent as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no such published agent"
        ) from exc
    return CreateSubscriptionResponse(
        id=sub.id,
        agent_slug=sub.target_slug,
        signature_scheme=sub.signature_scheme,
        webhook_path=f"/v1/webhooks/{sub.id}",
        signing_secret=secret,
    )


@router.get("/v1/webhook-subscriptions", response_model=list[SubscriptionOut])
async def list_subscriptions(
    member: MemberDep, service: WebhookSubscriptionServiceDep
) -> list[SubscriptionOut]:
    subs = await service.list_subscriptions(organisation_id=member.organisation_id)
    return [SubscriptionOut.model_validate(s) for s in subs]


@router.delete(
    "/v1/webhook-subscriptions/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_subscription(
    subscription_id: uuid.UUID, admin: AdminDep, service: WebhookSubscriptionServiceDep
) -> Response:
    ok = await service.delete(
        organisation_id=admin.organisation_id, subscription_id=subscription_id
    )
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
