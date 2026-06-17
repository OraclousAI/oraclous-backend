"""Unit: the gateway web-lifespan RLS role assertion + the two-engine repo wiring (ADR-030 §3).

The deployed gateway api connects on the org-bound oraclous_app DSN; a mis-deployed superuser /
BYPASSRLS role would silently void the FORCE'd RLS policy (T1-M3). The lifespan asserts a
NOSUPERUSER/NOBYPASSRLS role at startup, gated on GATEWAY_RLS_ASSERT_RUNTIME_ROLE, and refuses to
come up otherwise. These tests prove the three contractual behaviours WITHOUT a real Postgres (the
engine factory + the substrate assertion are patched), plus that the lifespan wires the two
OWNER-engine producer repos alongside the four org-bound repos. The gateway runs no Celery worker
that touches these tables, so the web lifespan is the sole assertion chokepoint (no worker mirror).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from oraclous_application_gateway_service.core import lifespan as lifespan_mod
from oraclous_application_gateway_service.core.rls import RlsBypassingRoleError

pytestmark = pytest.mark.unit


def _patch_settings(monkeypatch: pytest.MonkeyPatch, *, assert_on: bool) -> None:
    monkeypatch.setattr(
        lifespan_mod,
        "get_settings",
        lambda: SimpleNamespace(
            GATEWAY_RLS_ASSERT_RUNTIME_ROLE=assert_on,
            DATABASE_URL="postgresql+asyncpg://oraclous_app:app@localhost/oraclous",
            owner_database_url="postgresql+asyncpg://oraclous:oraclous@localhost/oraclous",
            # the rest of the lifespan reads these; harmless values so the body can run past the
            # assertion when the flag is on+isolating / off.
            UPSTREAM_CONNECT_TIMEOUT=5.0,
            UPSTREAM_READ_TIMEOUT=30.0,
            REDIS_URL="redis://localhost:6379/2",
            REDIS_SOCKET_TIMEOUT_SECONDS=0.5,
            INTERNAL_SERVICE_KEY="k",
        ),
    )


def _patch_body_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the non-RLS lifespan body deps (route table / proxy / Redis) so the test exercises ONLY
    the RLS assertion + the repo wiring, without opening real upstream/Redis connections."""
    monkeypatch.setattr(lifespan_mod, "build_route_table", lambda _s: MagicMock())
    monkeypatch.setattr(lifespan_mod, "ProxyService", lambda **_k: MagicMock())
    monkeypatch.setattr(lifespan_mod, "UpstreamClient", lambda _c: MagicMock())
    # Redis is aclose()'d in the finally — give the stub an awaitable aclose.
    redis_stub = MagicMock(aclose=AsyncMock())
    monkeypatch.setattr(lifespan_mod.aioredis, "from_url", lambda *_a, **_k: redis_stub)


def _patch_assert(monkeypatch: pytest.MonkeyPatch, assertion: Any) -> MagicMock:
    """Patch the org-bound assertion engine factory + the role assertion; return the fake engine."""
    engine = MagicMock(name="engine", dispose=AsyncMock())
    monkeypatch.setattr(lifespan_mod, "build_rls_engine", lambda _dsn: engine)
    monkeypatch.setattr(lifespan_mod, "assert_runtime_role_isolates", assertion)
    return engine


async def test_flag_off_skips_the_assertion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (owner DSN dev/test): no assertion engine is built; the app boots normally."""
    _patch_settings(monkeypatch, assert_on=False)
    _patch_body_deps(monkeypatch)
    built = MagicMock(side_effect=AssertionError("must not build the assertion engine when off"))
    monkeypatch.setattr(lifespan_mod, "build_rls_engine", built)
    # stub the repos so the lifespan body does not open real engines
    for cls in (
        "IntegrationKeyRepository",
        "PublishedAgentRepository",
        "ChatRepository",
        "WebhookSubscriptionRepository",
    ):
        monkeypatch.setattr(lifespan_mod, cls, lambda *_a, **_k: SimpleNamespace(close=AsyncMock()))

    app = FastAPI()
    async with lifespan_mod.lifespan(app):
        pass  # boots without building the assertion engine

    built.assert_not_called()


async def test_flag_on_bypassing_role_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gateway whose org-bound DSN is mis-deployed on a bypassing (owner) role must refuse to come
    up — SystemExit — and dispose the assertion engine."""
    _patch_settings(monkeypatch, assert_on=True)
    engine = _patch_assert(
        monkeypatch, AsyncMock(side_effect=RlsBypassingRoleError("rolsuper=True"))
    )

    app = FastAPI()
    with pytest.raises(SystemExit) as exc_info:
        async with lifespan_mod.lifespan(app):
            pass

    assert exc_info.value.code == 1
    engine.dispose.assert_awaited_once()  # disposed even on the failure path


async def test_flag_on_isolating_role_boots_and_wires_two_engines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployed NOSUPERUSER oraclous_app org-bound role passes the assertion; the app boots AND
    wires the two OWNER-engine producer repos (install_guard=False) alongside the four org-bound
    repos."""
    _patch_settings(monkeypatch, assert_on=True)
    _patch_body_deps(monkeypatch)
    assert_engine = MagicMock(name="assert_engine", dispose=AsyncMock())
    monkeypatch.setattr(lifespan_mod, "assert_runtime_role_isolates", AsyncMock(return_value=None))

    built: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _record(*args: Any, **kwargs: Any) -> Any:
        built.append((args, kwargs))
        return SimpleNamespace(close=AsyncMock())

    # build_rls_engine is used ONLY for the assertion engine here; the repos are stubbed to record
    # their (dsn, install_guard) so we can assert the two-engine carve.
    monkeypatch.setattr(lifespan_mod, "build_rls_engine", lambda _dsn: assert_engine)
    for cls in (
        "IntegrationKeyRepository",
        "PublishedAgentRepository",
        "ChatRepository",
        "WebhookSubscriptionRepository",
    ):
        monkeypatch.setattr(lifespan_mod, cls, _record)

    app = FastAPI()
    async with lifespan_mod.lifespan(app):
        # the two OWNER-engine producer repos are wired (used by the pre-auth producer reads).
        assert app.state.integration_key_owner_repo is not None
        assert app.state.webhook_subscription_owner_repo is not None
        assert app.state.integration_key_repo is not None
        assert app.state.webhook_subscription_repo is not None

    assert_engine.dispose.assert_awaited_once()
    # exactly two repos were built with install_guard=False (the OWNER-engine producers).
    owner_built = [kw for _a, kw in built if kw.get("install_guard") is False]
    assert len(owner_built) == 2
