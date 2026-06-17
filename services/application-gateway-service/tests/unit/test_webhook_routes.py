"""Unit: the webhook routes — public signed ingress + member-only subscription CRUD (R6 Slice 7).

Drives create_app with fake ingress/subscription services + a fake key repo (for the member-only
check). /v1/webhooks/{id} is public (no bearer); /v1/webhook-subscriptions is member-only.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_application_gateway_service.app.factory import create_app
from oraclous_application_gateway_service.core.dependencies import (
    get_webhook_ingress_service,
    get_webhook_subscription_service,
)
from oraclous_application_gateway_service.domain.integration_key import mint_key
from oraclous_application_gateway_service.services.webhook_ingress_service import (
    SubscriptionNotFound,
    UpstreamEngineError,
)
from oraclous_application_gateway_service.services.webhook_subscription_service import UnknownAgent

pytestmark = pytest.mark.unit

_DEV_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")
_DEV = {"authorization": "Bearer dev-token"}


class _FakeIngress:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.called = False

    async def ingest(self, **_kw):  # noqa: ANN003
        self.called = True
        if self._raises is not None:
            raise self._raises


class _FakeSubs:
    def __init__(self, *, unknown: bool = False) -> None:
        self._unknown = unknown
        self.deleted: list[uuid.UUID] = []
        self._rows = [
            SimpleNamespace(
                id=uuid.uuid4(),
                target_slug="weather",
                signature_scheme="generic",
                enabled=True,
                created_at=None,
            )
        ]

    async def create(self, *, organisation_id, agent_slug, signature_scheme="generic"):  # noqa: ANN001
        if self._unknown:
            raise UnknownAgent(agent_slug)
        sub = SimpleNamespace(id=uuid.uuid4(), target_slug=agent_slug, signature_scheme="generic")
        return sub, "whsec_secret_value"

    async def list_subscriptions(self, *, organisation_id, limit=100, offset=0):  # noqa: ANN001
        return self._rows[offset : offset + limit]

    async def delete(self, *, organisation_id, subscription_id):  # noqa: ANN001
        if subscription_id == self._rows[0].id:
            self.deleted.append(subscription_id)
            return True
        return False


def _app(*, ingress=None, subs=None):  # noqa: ANN001
    from oraclous_application_gateway_service.core.config import get_settings

    get_settings.cache_clear()
    app = create_app(lifespan=None)
    app.dependency_overrides[get_webhook_ingress_service] = lambda: ingress or _FakeIngress()
    app.dependency_overrides[get_webhook_subscription_service] = lambda: subs or _FakeSubs()
    return app


def _client(app):  # noqa: ANN001
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test")


# ── inbound (public, signature-gated) ──────────────────────────────────────────────────────────
async def test_inbound_no_bearer_succeeds_202() -> None:
    sid = uuid.uuid4()
    async with _client(_app()) as c:
        r = await c.post(f"/v1/webhooks/{sid}", content=b"{}")  # NO Authorization header
    assert r.status_code == 202


async def test_inbound_auth_failure_is_404() -> None:
    app = _app(ingress=_FakeIngress(raises=SubscriptionNotFound()))
    async with _client(app) as c:
        r = await c.post(f"/v1/webhooks/{uuid.uuid4()}", content=b"{}")
    assert r.status_code == 404


async def test_inbound_engine_unreachable_is_502() -> None:
    app = _app(ingress=_FakeIngress(raises=UpstreamEngineError("x")))
    async with _client(app) as c:
        r = await c.post(f"/v1/webhooks/{uuid.uuid4()}", content=b"{}")
    assert r.status_code == 502


# ── member CRUD (member-only) ──────────────────────────────────────────────────────────────────
async def test_member_create_returns_the_secret_once() -> None:
    async with _client(_app()) as c:
        r = await c.post("/v1/webhook-subscriptions", json={"agent_slug": "weather"}, headers=_DEV)
    assert r.status_code == 201
    body = r.json()
    assert body["signing_secret"] == "whsec_secret_value"  # noqa: S105
    assert body["webhook_path"] == f"/v1/webhooks/{body['id']}"


async def test_member_create_unknown_agent_is_404() -> None:
    async with _client(_app(subs=_FakeSubs(unknown=True))) as c:
        r = await c.post("/v1/webhook-subscriptions", json={"agent_slug": "nope"}, headers=_DEV)
    assert r.status_code == 404


class _FakeKeys:
    def __init__(self, row) -> None:  # noqa: ANN001
        self._row = row

    async def get_by_prefix(self, key_prefix):  # noqa: ANN001
        return self._row


async def test_member_crud_is_member_only_not_a_key() -> None:
    app = _app()
    minted = mint_key("oak")
    # the pre-auth get_by_prefix producer reads the OWNER-engine repo (ADR-030 §3); a fake has no
    # RLS so the same instance serves both.
    app.state.integration_key_owner_repo = _FakeKeys(
        SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=_DEV_ORG,
            key_prefix=minted.key_prefix,
            key_hash=minted.key_hash,
            status="active",
            expires_at=None,
            bound_agent_slug=None,
            capability_allow_list=None,
            cors_origins=None,
        )
    )
    async with _client(app) as c:
        r = await c.post(
            "/v1/webhook-subscriptions",
            json={"agent_slug": "weather"},
            headers={"authorization": f"Bearer {minted.plaintext}"},
        )
    assert r.status_code == 403  # an integration key cannot manage subscriptions


async def test_member_list_and_delete() -> None:
    subs = _FakeSubs()
    app = _app(subs=subs)
    async with _client(app) as c:
        lst = await c.get("/v1/webhook-subscriptions", headers=_DEV)
        assert lst.status_code == 200 and len(lst.json()) == 1
        gone = await c.delete(f"/v1/webhook-subscriptions/{subs._rows[0].id}", headers=_DEV)
        assert gone.status_code == 204
        missing = await c.delete(f"/v1/webhook-subscriptions/{uuid.uuid4()}", headers=_DEV)
        assert missing.status_code == 404
