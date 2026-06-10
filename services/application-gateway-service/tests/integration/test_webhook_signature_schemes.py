"""Integration: per-provider webhook signature schemes end-to-end vs real Postgres (#233).

Drives the REAL public ingress endpoint ``POST /v1/webhooks/{subscription_id}`` against a real
Postgres-backed subscription + published-agent store, for each pinned scheme (github / stripe /
slack):
  - a CORRECTLY-signed inbound payload verifies and fires the engine event (202);
  - a BADLY-signed payload (and one signed under a DIFFERENT scheme) is rejected as a uniform 404
    and NEVER fires the engine.

The only stubs are the two external seams the ingress depends on (not the surface under test): the
broker secret-client (resolve → a known signing secret) and the upstream engine client (captures the
fire instead of making a network call). The signature dispatch, the DB lookups, the raw-body
verification and the fail-closed 404 are all real. Stripe/slack sign against the live clock the
service reads (``int(time.time())``), well inside the ±5-min replay window.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")
_SLUG = "weather"
_CAP_REF = "cap-weather-1"
_SECRET = "whsec_integration_test"  # noqa: S105 — the (stubbed) broker-resolved signing secret
_BODY = b'{"event":"push","n":1}'
_INTERNAL_KEY = "ik-test"


# --- the two external seams the ingress depends on (stubbed; NOT the surface under test) ----------
class _StubSecrets:
    """Stands in for the cred-broker secret-resolve (the secret never lives in the gateway)."""

    async def resolve(self, *, organisation_id: uuid.UUID, secret_id: uuid.UUID) -> str:  # noqa: ARG002
        return _SECRET


class _Resp:
    def __init__(self, code: int) -> None:
        self.status_code = code

    async def aread(self) -> bytes:
        return b""

    async def aclose(self) -> None:
        return None


class _CapturingUpstream:
    """Captures the engine-event fire instead of opening a real connection."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def open(self, **kw):  # noqa: ANN003
        self.calls.append(kw)
        return _Resp(202)


@pytest.fixture
async def harness(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", _INTERNAL_KEY)

    from oraclous_application_gateway_service.core.config import get_settings

    get_settings.cache_clear()

    from oraclous_application_gateway_service.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    setup_engine = create_async_engine(async_dsn)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    from oraclous_application_gateway_service.app.factory import create_app
    from oraclous_application_gateway_service.core.dependencies import get_webhook_ingress_service
    from oraclous_application_gateway_service.repositories.published_agent_repository import (
        PublishedAgentRepository,
    )
    from oraclous_application_gateway_service.repositories.webhook_subscription_repository import (
        WebhookSubscriptionRepository,
    )
    from oraclous_application_gateway_service.services.webhook_ingress_service import (
        WebhookIngressService,
    )

    subs_repo = WebhookSubscriptionRepository(async_dsn)
    agents_repo = PublishedAgentRepository(async_dsn)
    # one active published agent the subscriptions all fire
    await agents_repo.create(organisation_id=_ORG, slug=_SLUG, bound_capability_ref=_CAP_REF)

    upstream = _CapturingUpstream()
    app = create_app(lifespan=None)

    def _ingress() -> WebhookIngressService:
        return WebhookIngressService(
            subscriptions=subs_repo,
            agents=agents_repo,
            secret_client=_StubSecrets(),
            upstream_client=upstream,
            engine_base_url="http://engine",
            internal_key=_INTERNAL_KEY,
            redis=None,  # per-sub limiter fails open; no Redis needed for these
        )

    app.dependency_overrides[get_webhook_ingress_service] = _ingress

    async def _new_sub(scheme: str) -> uuid.UUID:
        sub = await subs_repo.create(
            organisation_id=_ORG,
            target_slug=_SLUG,
            broker_secret_ref=uuid.uuid4(),
            signature_scheme=scheme,
        )
        return sub.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as c:
        yield {"client": c, "new_sub": _new_sub, "upstream": upstream}

    await subs_repo.close()
    await agents_repo.close()
    get_settings.cache_clear()


# --- per-scheme signing helpers (mirror the unit suite's wire formats) ----------------------------
def _hex(secret: str, msg: bytes) -> str:
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def _github_headers(body: bytes) -> dict:
    return {"x-hub-signature-256": "sha256=" + _hex(_SECRET, body)}


def _stripe_headers(body: bytes, now: int) -> dict:
    sig = _hex(_SECRET, f"{now}.".encode() + body)
    return {"stripe-signature": f"t={now},v1={sig}"}


def _slack_headers(body: bytes, now: int) -> dict:
    base = b"v0:" + str(now).encode() + b":" + body
    return {"x-slack-signature": "v0=" + _hex(_SECRET, base), "x-slack-request-timestamp": str(now)}


async def _post(client: AsyncClient, sub_id: uuid.UUID, headers: dict, body: bytes = _BODY):  # noqa: ANN202
    return await client.post(f"/v1/webhooks/{sub_id}", content=body, headers=headers)


# --- github -------------------------------------------------------------------------------------
async def test_github_valid_signature_fires(harness: dict) -> None:
    sub_id = await harness["new_sub"]("github")
    r = await _post(harness["client"], sub_id, _github_headers(_BODY))
    assert r.status_code == 202, r.text
    assert len(harness["upstream"].calls) == 1  # the engine event fired


async def test_github_bad_signature_is_404_and_does_not_fire(harness: dict) -> None:
    sub_id = await harness["new_sub"]("github")
    r = await _post(harness["client"], sub_id, {"x-hub-signature-256": "sha256=deadbeef"})
    assert r.status_code == 404, r.text
    assert harness["upstream"].calls == []


# --- stripe -------------------------------------------------------------------------------------
async def test_stripe_valid_signature_fires(harness: dict) -> None:
    sub_id = await harness["new_sub"]("stripe")
    r = await _post(harness["client"], sub_id, _stripe_headers(_BODY, int(time.time())))
    assert r.status_code == 202, r.text
    assert len(harness["upstream"].calls) == 1


async def test_stripe_tampered_body_is_404(harness: dict) -> None:
    sub_id = await harness["new_sub"]("stripe")
    # signature computed over _BODY but a DIFFERENT body is sent → fail-closed
    r = await _post(
        harness["client"], sub_id, _stripe_headers(_BODY, int(time.time())), body=_BODY + b"x"
    )
    assert r.status_code == 404, r.text
    assert harness["upstream"].calls == []


# --- slack --------------------------------------------------------------------------------------
async def test_slack_valid_signature_fires(harness: dict) -> None:
    sub_id = await harness["new_sub"]("slack")
    r = await _post(harness["client"], sub_id, _slack_headers(_BODY, int(time.time())))
    assert r.status_code == 202, r.text
    assert len(harness["upstream"].calls) == 1


async def test_slack_bad_signature_is_404(harness: dict) -> None:
    sub_id = await harness["new_sub"]("slack")
    bad = {"x-slack-signature": "v0=deadbeef", "x-slack-request-timestamp": str(int(time.time()))}
    r = await _post(harness["client"], sub_id, bad)
    assert r.status_code == 404, r.text
    assert harness["upstream"].calls == []


# --- no cross-scheme acceptance -----------------------------------------------------------------
async def test_a_scheme_rejects_another_schemes_signature(harness: dict) -> None:
    # a subscription pinned to STRIPE must not accept a (valid) github-style generic signature
    sub_id = await harness["new_sub"]("stripe")
    r = await _post(harness["client"], sub_id, _github_headers(_BODY))
    assert r.status_code == 404, r.text
    assert harness["upstream"].calls == []
