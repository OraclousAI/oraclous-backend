"""#1072 (PR #1094 review, QA finding C2) — nothing today proves the cancel lease repository is
actually wired end to end. QA proved the gap live: swapping ``core/dependencies.py``'s
``get_harness_service`` to hardcode ``leases=None``, or making ``core/lifespan.py`` never store the
repository on ``app.state``, left every harness-runtime unit, lease-integration and RLS test green
(1046/1046). Either mutation means a timed-out member's loop keeps running and spending, and every
cancel call either 404s (no watcher ever ran) or 503s (``get_lease_repository`` finds nothing on
``app.state``) — cancellation goes silently dead in production with zero test signal, the same class
of bug as #968.

Drives the REAL provider chain (``lifespan`` + ``get_lease_repository`` + ``get_harness_service``),
never a hand-rolled fake of either, against a non-connecting DSN — the repository's SQLAlchemy async
engine is lazy, so binding it never opens a socket (the same technique as
``test_fake_llm_failclosed.py``'s ``_env`` fixture, which already relies on this to keep that suite
a unit test).

A plain ``SimpleNamespace(app=app)`` stands in for FastAPI's ``Request``: every provider this test
calls directly (``get_lease_repository``, and — through ``get_harness_service`` — none of the
others, since this test supplies their resolved values itself) reads only ``request.app.state``, so
the stand-in is faithful to the real request-scoped call.

Not-yet-built seams, imported function-locally per ``.claude/rules/tests-seam-imports.md`` (today's
``lifespan`` never sets ``app.state.lease_repository`` at all, so it stays ``None``/absent;
``get_lease_repository`` and the ``ExecutionLeaseRepository`` module do not exist; and
``get_harness_service``'s signature has no ``leases`` parameter, so passing one is a ``TypeError``):
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from oraclous_harness_runtime_service.app.factory import create_app
from oraclous_harness_runtime_service.core.config import get_settings
from oraclous_ohm.signatures import TrustStore

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    # A non-connecting DSN: SQLAlchemy's async engine is lazy, so lifespan binds every repository
    # (including the lease repo, once it exists) without ever opening a socket.
    monkeypatch.setenv("HARNESS_DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.delenv("EXIT_ON_STARTUP_DEGRADE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_lifespan_wires_a_real_lease_repository_onto_app_state() -> None:
    """core/lifespan.py:54 gap: lifespan must construct the lease repository alongside the other
    three tenant-scoped repos and store it on app.state, exactly like it already does for
    execution/assignment/checkpoint. RED today: app.state has no lease_repository attribute at
    all, so it stays the sentinel default rather than becoming a real repository instance."""
    from oraclous_harness_runtime_service.core import lifespan as lifespan_module
    from oraclous_harness_runtime_service.repositories.execution_lease_repository import (
        ExecutionLeaseRepository,
    )

    app = create_app()
    async with lifespan_module.lifespan(app):
        assert isinstance(app.state.lease_repository, ExecutionLeaseRepository)


async def test_get_harness_service_forwards_the_app_state_lease_repository() -> None:
    """core/dependencies.py:249 gap: get_harness_service must forward the lease repository it is
    handed into HarnessExecutionService(leases=...) rather than silently dropping it (a hardcoded
    leases=None reads as valid pre-#1072 shape and produces no error anywhere). Drives
    get_harness_service directly with the SAME repository get_lease_repository would resolve off
    app.state, so this pins the wire, not just that the constructor accepts the kwarg."""
    from oraclous_harness_runtime_service.core import lifespan as lifespan_module
    from oraclous_harness_runtime_service.core.dependencies import (
        get_harness_service,
        get_lease_repository,
    )

    app = create_app()
    async with lifespan_module.lifespan(app):
        request = SimpleNamespace(app=app)

        # RED today: no such provider — AttributeError/ImportError.
        leases = get_lease_repository(request)

        service = get_harness_service(
            registry=object(),
            broker=object(),
            executions=object(),
            assignments=object(),
            checkpoints=object(),
            provenance=object(),
            trust=TrustStore({}),
            memory=None,
            memory_reader=None,
            # RED today: get_harness_service() takes no `leases` kwarg — TypeError.
            leases=leases,
        )

        assert service._leases is leases
        assert service._leases is not None
