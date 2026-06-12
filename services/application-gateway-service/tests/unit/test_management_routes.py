"""Unit: the Slice-4 management plane — publish agents + integration-key CRUD, member-org-scoped.

Drives the real app (create_app) with in-memory fake repos on app.state, using the dev bearer (a
USER principal in the dev org). Asserts: member-only (a key bearer is 403), the mint binding rules,
unknown-slug 404, redaction on list, rotate/revoke, and org-scoping (a row from another org is 404).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_application_gateway_service.app.factory import create_app
from oraclous_application_gateway_service.domain.integration_key import mint_key

pytestmark = pytest.mark.unit

_DEV_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")  # matches DEV_ORG_ID


class _FakeAgents:
    def __init__(self) -> None:
        self.rows: list = []

    async def get_by_slug(self, *, organisation_id, slug):  # noqa: ANN001
        return next(
            (r for r in self.rows if r.organisation_id == organisation_id and r.slug == slug), None
        )

    async def create(
        self, *, organisation_id, slug, bound_capability_ref, display_name=None, description=None
    ):  # noqa: ANN001
        row = SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            slug=slug,
            bound_capability_ref=bound_capability_ref,
            display_name=display_name,
            description=description,
            status="active",
            created_at=datetime.now(UTC),
        )
        self.rows.append(row)
        return row

    async def list_for_org(self, organisation_id):  # noqa: ANN001
        return [r for r in self.rows if r.organisation_id == organisation_id]

    async def unpublish(self, *, organisation_id, slug):  # noqa: ANN001
        row = await self.get_by_slug(organisation_id=organisation_id, slug=slug)
        if row is not None:
            row.status = "unpublished"
        return row


class _FakeKeys:
    def __init__(self) -> None:
        self.rows: list = []

    async def get_by_prefix(self, key_prefix):  # noqa: ANN001 — used by the S3 validator on a key bearer
        return next((r for r in self.rows if r.key_prefix == key_prefix), None)

    async def create(
        self,
        *,
        organisation_id,
        key_prefix,
        key_hash,
        last4,
        bound_agent_slug=None,
        capability_allow_list=None,
        cors_origins=None,
        rate_limit=None,
        rate_window_seconds=None,
        expires_at=None,
    ):  # noqa: ANN001
        row = SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            key_prefix=key_prefix,
            key_hash=key_hash,
            last4=last4,
            bound_agent_slug=bound_agent_slug,
            capability_allow_list=capability_allow_list,
            cors_origins=cors_origins,
            rate_limit=rate_limit,
            rate_window_seconds=rate_window_seconds,
            status="active",
            expires_at=expires_at,
            created_at=datetime.now(UTC),
        )
        self.rows.append(row)
        return row

    async def list_for_org(self, organisation_id):  # noqa: ANN001
        return [r for r in self.rows if r.organisation_id == organisation_id]

    async def get_for_org(self, *, key_id, organisation_id):  # noqa: ANN001
        return next(
            (r for r in self.rows if r.id == key_id and r.organisation_id == organisation_id), None
        )

    async def rotate(self, *, key_id, organisation_id, key_prefix, key_hash, last4):  # noqa: ANN001
        row = await self.get_for_org(key_id=key_id, organisation_id=organisation_id)
        if row is None or row.status != "active":  # a revoked key is terminal — not resurrected
            return None
        row.key_prefix, row.key_hash, row.last4 = key_prefix, key_hash, last4
        return row

    async def revoke(self, *, key_id, organisation_id):  # noqa: ANN001
        row = await self.get_for_org(key_id=key_id, organisation_id=organisation_id)
        if row is not None:
            row.status = "revoked"
        return row


def _app():
    from oraclous_application_gateway_service.core.config import get_settings

    get_settings.cache_clear()
    app = create_app(lifespan=None)
    app.state.integration_key_repo = _FakeKeys()
    app.state.published_agent_repo = _FakeAgents()
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test")


_DEV = {"authorization": "Bearer dev-token"}


async def test_publish_then_mint_then_list_rotate_revoke() -> None:
    app = _app()
    async with _client(app) as c:
        # publish an agent
        r = await c.post(
            "/v1/agents", json={"slug": "weather", "bound_capability_ref": "cap-123"}, headers=_DEV
        )
        assert r.status_code == 201, r.text
        assert r.json()["slug"] == "weather"
        # mint a key bound to it -> plaintext returned ONCE
        r = await c.post("/v1/integration-keys", json={"bound_agent_slug": "weather"}, headers=_DEV)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["key"].startswith("oak-") and body["bound_agent_slug"] == "weather"
        key_id = body["id"]
        # list -> redacted (the plaintext/hash never appear)
        r = await c.get("/v1/integration-keys", headers=_DEV)
        assert r.status_code == 200
        listed = r.json()[0]
        assert "key" not in listed and "key_hash" not in listed
        assert listed["last4"] and listed["status"] == "active"
        # rotate -> a NEW plaintext
        r = await c.post(f"/v1/integration-keys/{key_id}/rotate", headers=_DEV)
        assert r.status_code == 200 and r.json()["key"].startswith("oak-")
        assert r.json()["key"] != body["key"]
        # revoke -> 204, then status revoked
        r = await c.delete(f"/v1/integration-keys/{key_id}", headers=_DEV)
        assert r.status_code == 204
        r = await c.get(f"/v1/integration-keys/{key_id}", headers=_DEV)
        assert r.json()["status"] == "revoked"


async def test_unpublish_flips_status_and_is_idempotent() -> None:
    # DELETE /v1/agents/{slug} -> 204; the row is soft-tombstoned to 'unpublished'; a second call is
    # idempotent (still 204, still unpublished) since the slug still resolves in the org.
    app = _app()
    async with _client(app) as c:
        r = await c.post(
            "/v1/agents", json={"slug": "weather", "bound_capability_ref": "cap-1"}, headers=_DEV
        )
        assert r.status_code == 201, r.text
        # list shows it active
        assert (await c.get("/v1/agents", headers=_DEV)).json()[0]["status"] == "active"
        # unpublish -> 204
        r = await c.delete("/v1/agents/weather", headers=_DEV)
        assert r.status_code == 204
        assert (await c.get("/v1/agents", headers=_DEV)).json()[0]["status"] == "unpublished"
        # idempotent: the slug still resolves, so a re-unpublish is 204, not 404
        r = await c.delete("/v1/agents/weather", headers=_DEV)
        assert r.status_code == 204
        assert (await c.get("/v1/agents", headers=_DEV)).json()[0]["status"] == "unpublished"


async def test_unpublish_unknown_slug_is_404() -> None:
    app = _app()
    async with _client(app) as c:
        r = await c.delete("/v1/agents/nope", headers=_DEV)
    assert r.status_code == 404


async def test_unpublish_requires_a_member_credential() -> None:
    # an integration-key (SERVICE_ACCOUNT) bearer must be 403 on the admin-only unpublish
    app = _app()
    minted = mint_key("oak")
    app.state.integration_key_repo.rows.append(
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
        r = await c.delete(
            "/v1/agents/weather", headers={"authorization": f"Bearer {minted.plaintext}"}
        )
    assert r.status_code == 403


async def test_unpublish_org_scoped_other_org_slug_is_404() -> None:
    # an agent published in another org must not be unpublishable from the dev org
    app = _app()
    app.state.published_agent_repo.rows.append(
        SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),  # not the dev org
            slug="weather",
            bound_capability_ref="cap-1",
            display_name=None,
            description=None,
            status="active",
            created_at=datetime.now(UTC),
        )
    )
    async with _client(app) as c:
        r = await c.delete("/v1/agents/weather", headers=_DEV)
    assert r.status_code == 404
    # the other-org row is untouched
    assert app.state.published_agent_repo.rows[0].status == "active"


async def test_rotate_does_not_resurrect_a_revoked_key() -> None:
    # revoke is terminal: rotating a revoked key must 404, never flip it back to active
    app = _app()
    async with _client(app) as c:
        await c.post("/v1/agents", json={"slug": "a", "bound_capability_ref": "c"}, headers=_DEV)
        mid = (
            await c.post("/v1/integration-keys", json={"bound_agent_slug": "a"}, headers=_DEV)
        ).json()["id"]
        assert (await c.delete(f"/v1/integration-keys/{mid}", headers=_DEV)).status_code == 204
        rot = await c.post(f"/v1/integration-keys/{mid}/rotate", headers=_DEV)
        assert rot.status_code == 404
        # still revoked
        assert (await c.get(f"/v1/integration-keys/{mid}", headers=_DEV)).json()[
            "status"
        ] == "revoked"


async def test_mint_unknown_slug_is_404() -> None:
    app = _app()
    async with _client(app) as c:
        r = await c.post("/v1/integration-keys", json={"bound_agent_slug": "nope"}, headers=_DEV)
    assert r.status_code == 404


async def test_mint_requires_exactly_one_binding() -> None:
    app = _app()
    async with _client(app) as c:
        both = await c.post(
            "/v1/integration-keys",
            json={"bound_agent_slug": "x", "capability_allow_list": ["c"]},
            headers=_DEV,
        )
        neither = await c.post("/v1/integration-keys", json={}, headers=_DEV)
    assert both.status_code == 422 and neither.status_code == 422


async def test_capability_key_needs_no_published_agent() -> None:
    app = _app()
    async with _client(app) as c:
        r = await c.post(
            "/v1/integration-keys", json={"capability_allow_list": ["cap:read"]}, headers=_DEV
        )
    assert r.status_code == 201 and r.json()["capability_allow_list"] == ["cap:read"]


async def test_a_key_bearer_cannot_manage_keys() -> None:
    # an integration-key (SERVICE_ACCOUNT) bearer must be 403 on the member-only CRUD
    app = _app()
    minted = mint_key("oak")
    app.state.integration_key_repo.rows.append(
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
        r = await c.get(
            "/v1/integration-keys", headers={"authorization": f"Bearer {minted.plaintext}"}
        )
    assert r.status_code == 403


async def test_no_auth_is_401() -> None:
    app = _app()
    async with _client(app) as c:
        r = await c.get("/v1/integration-keys")
    assert r.status_code == 401


async def test_org_scoping_other_org_key_is_404() -> None:
    app = _app()
    other = SimpleNamespace(
        id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        key_prefix="x",
        key_hash="h",
        last4="9999",
        bound_agent_slug=None,
        capability_allow_list=["c"],
        cors_origins=None,
        rate_limit=None,
        status="active",
        expires_at=None,
        created_at=datetime.now(UTC),
    )
    app.state.integration_key_repo.rows.append(other)
    async with _client(app) as c:
        r = await c.get(f"/v1/integration-keys/{other.id}", headers=_DEV)  # dev org != other org
    assert r.status_code == 404
