"""Unit: WebhookSubscriptionService — the orphan-secret GC (R7-SEC S4). No DB / broker."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from oraclous_application_gateway_service.services.webhook_subscription_service import (
    WebhookSubscriptionService,
)

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()


class _Agents:
    async def get_by_slug(self, *, organisation_id, slug):  # noqa: ANN001, ANN201, ARG002
        return SimpleNamespace(status="active")


class _Secrets:
    def __init__(self) -> None:
        self.minted: list[uuid.UUID] = []
        self.deleted: list[uuid.UUID] = []

    async def mint(self, *, organisation_id, secret):  # noqa: ANN001, ANN201, ARG002
        sid = uuid.uuid4()
        self.minted.append(sid)
        return sid

    async def delete(self, *, organisation_id, secret_id):  # noqa: ANN001, ANN201, ARG002
        self.deleted.append(secret_id)
        return True


class _Subs:
    def __init__(self, *, fail_create: bool = False, sub=None) -> None:  # noqa: ANN001
        self._fail = fail_create
        self._sub = sub
        self.deleted: list[uuid.UUID] = []

    async def create(
        self, *, organisation_id, target_slug, broker_secret_ref, signature_scheme="generic"
    ):  # noqa: ANN001, ANN201
        if self._fail:
            raise RuntimeError("insert failed")
        return SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            target_slug=target_slug,
            broker_secret_ref=broker_secret_ref,
            signature_scheme="generic",
        )

    async def get_by_id(self, subscription_id):  # noqa: ANN001, ANN201
        return self._sub

    async def delete_for_org(self, *, subscription_id, organisation_id):  # noqa: ANN001, ANN201
        self.deleted.append(subscription_id)
        return self._sub is not None and self._sub.organisation_id == organisation_id


def _svc(subs: _Subs, secrets: _Secrets) -> WebhookSubscriptionService:
    return WebhookSubscriptionService(subscriptions=subs, agents=_Agents(), secret_client=secrets)


async def test_create_compensates_the_secret_on_insert_failure() -> None:
    secrets = _Secrets()
    with pytest.raises(RuntimeError):
        await _svc(_Subs(fail_create=True), secrets).create(organisation_id=_ORG, agent_slug="a")
    # the minted-but-orphaned secret is GC'd, and the real error still propagates
    assert secrets.deleted == secrets.minted and len(secrets.deleted) == 1


async def test_delete_drops_the_broker_secret() -> None:
    secret_ref = uuid.uuid4()
    sub = SimpleNamespace(id=uuid.uuid4(), organisation_id=_ORG, broker_secret_ref=secret_ref)
    secrets = _Secrets()
    ok = await _svc(_Subs(sub=sub), secrets).delete(organisation_id=_ORG, subscription_id=sub.id)
    assert ok is True
    assert secrets.deleted == [secret_ref]  # the now-unreferenced secret was GC'd


async def test_delete_of_a_cross_org_subscription_is_a_noop() -> None:
    # the sub belongs to a DIFFERENT org -> not found -> no row deleted, no secret touched
    sub = SimpleNamespace(
        id=uuid.uuid4(), organisation_id=uuid.uuid4(), broker_secret_ref=uuid.uuid4()
    )
    secrets = _Secrets()
    ok = await _svc(_Subs(sub=sub), secrets).delete(organisation_id=_ORG, subscription_id=sub.id)
    assert ok is False
    assert secrets.deleted == []
