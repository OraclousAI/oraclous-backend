"""Unit: WebhookIngressService — verify + fire, with the whole auth-failure family -> 404. No DB."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from types import SimpleNamespace

import pytest
from oraclous_application_gateway_service.services.webhook_ingress_service import (
    SubscriptionNotFound,
    UpstreamEngineError,
    WebhookIngressService,
)

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_SECRET_REF = uuid.uuid4()
_SECRET = "whsec_live"  # noqa: S105 — test fixture
_BODY = b'{"event":"push"}'


def _sign(body: bytes, secret: str = _SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class _Subs:
    def __init__(self, sub) -> None:  # noqa: ANN001
        self._sub = sub

    async def get_by_id(self, subscription_id):  # noqa: ANN001
        return self._sub if self._sub and self._sub.id == subscription_id else None


class _Agents:
    def __init__(self, *, active: bool = True) -> None:
        self._active = active

    async def get_by_slug(self, *, organisation_id, slug):  # noqa: ANN001
        if not self._active:
            return None
        return SimpleNamespace(bound_capability_ref="cap-1", status="active")


class _Secrets:
    def __init__(self, *, secret: str | None = _SECRET) -> None:
        self._secret = secret

    async def resolve(self, *, organisation_id, secret_id):  # noqa: ANN001
        return self._secret


class _Resp:
    def __init__(self, code: int) -> None:
        self.status_code = code

    async def aread(self) -> bytes:
        return b""

    async def aclose(self) -> None:
        return None


class _Upstream:
    def __init__(self, code: int = 202) -> None:
        self.code = code
        self.calls: list[dict] = []

    async def open(self, **kw):  # noqa: ANN001, ANN003
        self.calls.append(kw)
        return _Resp(self.code)


def _sub(*, enabled: bool = True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        broker_secret_ref=_SECRET_REF,
        target_slug="weather",
        enabled=enabled,
        signature_scheme="generic",
    )


def _service(*, sub, agents=None, secrets=None, upstream=None) -> WebhookIngressService:  # noqa: ANN001
    return WebhookIngressService(
        subscriptions=_Subs(sub),
        agents=agents or _Agents(),
        secret_client=secrets or _Secrets(),
        upstream_client=upstream or _Upstream(),
        engine_base_url="http://engine",
        internal_key="k",
    )


async def test_valid_webhook_fires_an_engine_event() -> None:
    sub = _sub()
    up = _Upstream(202)
    svc = _service(sub=sub, upstream=up)
    await svc.ingest(
        subscription_id=sub.id, raw_body=_BODY, signature_header=_sign(_BODY), delivery_id="d-1"
    )
    assert len(up.calls) == 1
    call = up.calls[0]
    assert call["url"] == "http://engine/v1/engine/events"
    body = json.loads(call["content"])
    assert body["manifest_ref"] == "cap-1" and body["idempotency_key"] == "d-1"
    # the org is asserted via the forwarded trusted headers, never the event body
    assert b"x-organisation-id" in {k.lower() for k, _ in call["headers"]}


async def test_no_delivery_id_dedupes_on_the_body_hash() -> None:
    sub = _sub()
    up = _Upstream(202)
    await _service(sub=sub, upstream=up).ingest(
        subscription_id=sub.id, raw_body=_BODY, signature_header=_sign(_BODY), delivery_id=None
    )
    body = json.loads(up.calls[0]["content"])
    assert body["idempotency_key"] == hashlib.sha256(_BODY).hexdigest()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda s: setattr(s, "enabled", False), id="disabled"),
        pytest.param(lambda s: setattr(s, "id", uuid.uuid4()), id="unknown-id"),
    ],
)
async def test_unknown_or_disabled_subscription_is_404(mutate) -> None:  # noqa: ANN001
    sub = _sub()
    target = sub.id
    mutate(sub)
    with pytest.raises(SubscriptionNotFound):
        await _service(sub=sub).ingest(
            subscription_id=target, raw_body=_BODY, signature_header=_sign(_BODY), delivery_id=None
        )


async def test_bad_signature_is_404_and_does_not_fire() -> None:
    sub = _sub()
    up = _Upstream()
    with pytest.raises(SubscriptionNotFound):
        await _service(sub=sub, upstream=up).ingest(
            subscription_id=sub.id, raw_body=_BODY, signature_header="sha256=bad", delivery_id=None
        )
    assert up.calls == []  # never forwarded


async def test_unresolvable_secret_is_404() -> None:
    sub = _sub()
    with pytest.raises(SubscriptionNotFound):
        await _service(sub=sub, secrets=_Secrets(secret=None)).ingest(
            subscription_id=sub.id, raw_body=_BODY, signature_header=_sign(_BODY), delivery_id=None
        )


async def test_unpublished_agent_is_404() -> None:
    sub = _sub()
    with pytest.raises(SubscriptionNotFound):
        await _service(sub=sub, agents=_Agents(active=False)).ingest(
            subscription_id=sub.id, raw_body=_BODY, signature_header=_sign(_BODY), delivery_id=None
        )


async def test_engine_non_2xx_is_502() -> None:
    sub = _sub()
    with pytest.raises(UpstreamEngineError):
        await _service(sub=sub, upstream=_Upstream(500)).ingest(
            subscription_id=sub.id, raw_body=_BODY, signature_header=_sign(_BODY), delivery_id=None
        )
