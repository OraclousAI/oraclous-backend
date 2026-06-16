"""Integration: workspace<->harness bindings vs real Postgres (Contract G2 / ADR-029).

Proves the binding endpoints end-to-end against a testcontainer Postgres, with the KGS membership
call STUBBED (no live knowledge-graph-service): attach 201 → idempotent re-attach 200 → attach an
unknown harness 404 → attach a graph not in the caller's accessible set 404 → list-by-graph and
list-by-harness shapes → detach 204 then 404 → cross-org access masked (404/empty). Key-free
(dev bearer + testcontainer Postgres); the membership set is injected per test.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_DEV_ORG = "00000000-0000-0000-0000-00000000050a"
_OTHER_ORG = "00000000-0000-0000-0000-0000000006ff"
_GRAPH_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_GRAPH_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_GRAPH_UNKNOWN = uuid.UUID("99999999-9999-9999-9999-999999999999")


@pytest.fixture
async def client(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    monkeypatch.setenv("DATABASE_URL", async_dsn)
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "dev-internal-key")
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("DEV_BEARER", "dev-token")
    monkeypatch.setenv("DEV_ORG_ID", _DEV_ORG)
    from oraclous_capability_registry_service.core.config import get_settings

    get_settings.cache_clear()

    from oraclous_capability_registry_service.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    setup_engine = create_async_engine(async_dsn)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    from oraclous_capability_registry_service.app.factory import create_app
    from oraclous_capability_registry_service.repositories.binding_repository import (
        BindingRepository,
    )
    from oraclous_capability_registry_service.repositories.capability_repository import (
        CapabilityRepository,
    )

    app = create_app(lifespan=None)
    repo = CapabilityRepository(async_dsn)
    binding_repo = BindingRepository(async_dsn)
    app.state.capability_repository = repo
    app.state.binding_repository = binding_repo
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cr.test") as c:
        yield c
    await repo.close()
    await binding_repo.close()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _stub_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the KGS membership: the caller's org "owns" GRAPH_A + GRAPH_B (named), nothing else.

    The registry can't read a graph's org, so the graph-side visibility check goes to KGS; with no
    live KGS we inject a deterministic accessible-set so the attach/list logic is exercised in full.
    """
    from oraclous_capability_registry_service.services import graph_membership_client

    async def _fake_accessible(self, *, organisation_id, user_id):  # noqa: ANN001, ANN202
        return {_GRAPH_A: "Acme support KB", _GRAPH_B: "Acme sales KB"}

    monkeypatch.setattr(
        graph_membership_client.GraphMembershipClient, "accessible_graphs", _fake_accessible
    )


def _auth(bearer: str = "dev-token") -> dict:
    return {"Authorization": f"Bearer {bearer}"}


async def _register_harness(client: AsyncClient, name: str = "Triage agent") -> str:
    """Register a kind:harness capability and return its id (the agent to bind)."""
    body = {
        "kind": "harness",
        "descriptor": {
            "kind": "harness",
            "metadata": {"name": name, "description": "routes inbound tickets"},
            "spec": {"type": "OHM"},
        },
    }
    created = await client.post("/api/v1/capabilities", json=body, headers=_auth())
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def test_attach_then_idempotent_reattach(client: AsyncClient) -> None:
    harness_id = await _register_harness(client)

    first = await client.post(
        "/api/v1/agent-bindings",
        json={"harness_id": harness_id, "graph_id": str(_GRAPH_A)},
        headers=_auth(),
    )
    assert first.status_code == 201, first.text
    assert first.json() == {"created": True}

    # re-attach the same pair → idempotent 200, NOT a 409.
    again = await client.post(
        "/api/v1/agent-bindings",
        json={"harness_id": harness_id, "graph_id": str(_GRAPH_A)},
        headers=_auth(),
    )
    assert again.status_code == 200, again.text
    assert again.json() == {"created": False}


async def test_attach_unknown_harness_is_404(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/agent-bindings",
        json={"harness_id": str(uuid.uuid4()), "graph_id": str(_GRAPH_A)},
        headers=_auth(),
    )
    assert resp.status_code == 404


async def test_attach_graph_not_in_org_is_404(client: AsyncClient) -> None:
    harness_id = await _register_harness(client)
    resp = await client.post(
        "/api/v1/agent-bindings",
        json={"harness_id": harness_id, "graph_id": str(_GRAPH_UNKNOWN)},
        headers=_auth(),
    )
    assert resp.status_code == 404


async def test_attach_non_harness_capability_is_404(client: AsyncClient) -> None:
    # a kind:tool capability is not an agent — binding it is a 404 (mask), never a 500.
    tool_body = {
        "kind": "tool",
        "descriptor": {
            "kind": "tool",
            "metadata": {"name": "Echo", "category": "UTILITY"},
            "spec": {"type": "INTERNAL", "capabilities": [{"name": "echo"}]},
        },
    }
    tool_id = (await client.post("/api/v1/capabilities", json=tool_body, headers=_auth())).json()[
        "id"
    ]
    resp = await client.post(
        "/api/v1/agent-bindings",
        json={"harness_id": tool_id, "graph_id": str(_GRAPH_A)},
        headers=_auth(),
    )
    assert resp.status_code == 404


async def test_list_by_graph_shape(client: AsyncClient) -> None:
    harness_id = await _register_harness(client, name="Triage agent")
    await client.post(
        "/api/v1/agent-bindings",
        json={"harness_id": harness_id, "graph_id": str(_GRAPH_A)},
        headers=_auth(),
    )
    resp = await client.get(f"/api/v1/agent-bindings?graph_id={_GRAPH_A}", headers=_auth())
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert rows == [
        {
            "harness_id": harness_id,
            "name": "Triage agent",
            "kind": "harness",
            "summary": "routes inbound tickets",
        }
    ]


async def test_list_by_harness_shape_filters_to_live_graphs(client: AsyncClient) -> None:
    harness_id = await _register_harness(client)
    # bind to GRAPH_A (live) ...
    await client.post(
        "/api/v1/agent-bindings",
        json={"harness_id": harness_id, "graph_id": str(_GRAPH_A)},
        headers=_auth(),
    )
    # ... and seed a DANGLING binding (a graph not in the membership set) directly in the repo.
    binding_repo = client._transport.app.state.binding_repository  # type: ignore[attr-defined]
    await binding_repo.attach(
        organisation_id=uuid.UUID(_DEV_ORG),
        harness_capability_id=uuid.UUID(harness_id),
        graph_id=_GRAPH_UNKNOWN,
        created_by=uuid.uuid4(),
    )

    resp = await client.get(f"/api/v1/agent-bindings?harness_id={harness_id}", headers=_auth())
    assert resp.status_code == 200, resp.text
    # only the LIVE graph is returned (the dangling one is lazily skipped, ADR-029 §4), named from
    # the membership set.
    assert resp.json() == [{"graph_id": str(_GRAPH_A), "name": "Acme support KB"}]


async def test_list_requires_exactly_one_filter(client: AsyncClient) -> None:
    # neither filter → 422
    assert (await client.get("/api/v1/agent-bindings", headers=_auth())).status_code == 422
    # both filters → 422
    both = await client.get(
        f"/api/v1/agent-bindings?graph_id={_GRAPH_A}&harness_id={uuid.uuid4()}", headers=_auth()
    )
    assert both.status_code == 422


async def test_detach_then_404(client: AsyncClient) -> None:
    harness_id = await _register_harness(client)
    await client.post(
        "/api/v1/agent-bindings",
        json={"harness_id": harness_id, "graph_id": str(_GRAPH_A)},
        headers=_auth(),
    )
    deleted = await client.delete(
        f"/api/v1/agent-bindings?harness_id={harness_id}&graph_id={_GRAPH_A}", headers=_auth()
    )
    assert deleted.status_code == 204

    # detaching again → 404 (no longer bound).
    again = await client.delete(
        f"/api/v1/agent-bindings?harness_id={harness_id}&graph_id={_GRAPH_A}", headers=_auth()
    )
    assert again.status_code == 404


async def test_detach_unknown_pair_is_404(client: AsyncClient) -> None:
    harness_id = await _register_harness(client)
    resp = await client.delete(
        f"/api/v1/agent-bindings?harness_id={harness_id}&graph_id={_GRAPH_B}", headers=_auth()
    )
    assert resp.status_code == 404


async def test_cross_org_binding_is_masked(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bind in the dev org ...
    harness_id = await _register_harness(client)
    await client.post(
        "/api/v1/agent-bindings",
        json={"harness_id": harness_id, "graph_id": str(_GRAPH_A)},
        headers=_auth(),
    )

    # ... then switch the caller to another org: the harness is no longer visible, so list-by-graph
    # is empty and detach 404s (org isolation, ADR-006).
    from oraclous_capability_registry_service.core.config import get_settings

    monkeypatch.setenv("DEV_ORG_ID", _OTHER_ORG)
    get_settings.cache_clear()
    try:
        listed = await client.get(f"/api/v1/agent-bindings?graph_id={_GRAPH_A}", headers=_auth())
        assert listed.status_code == 200
        assert listed.json() == []
        detached = await client.delete(
            f"/api/v1/agent-bindings?harness_id={harness_id}&graph_id={_GRAPH_A}", headers=_auth()
        )
        assert detached.status_code == 404
    finally:
        monkeypatch.setenv("DEV_ORG_ID", _DEV_ORG)
        get_settings.cache_clear()
