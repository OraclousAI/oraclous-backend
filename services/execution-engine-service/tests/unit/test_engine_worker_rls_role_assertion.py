"""Unit test for the worker-process-startup RLS role assertion (ADR-030 §3, worker mirror).

The Celery worker process never runs the FastAPI lifespan, so the web's fail-closed
NOSUPERUSER/NOBYPASSRLS role check would not protect it. The ``worker_process_init`` hook in
``celery_app`` adds that check for the worker process's ORG-BOUND engine — once per process, gated
on ``rls_assert_runtime_role``. These tests prove the three contractual behaviours WITHOUT a real
broker/Postgres: the engine factory + the substrate assertion are patched.

  * flag OFF (default) -> no engine built, no assertion, worker starts normally;
  * flag ON + bypassing role -> SystemExit (fail closed LOUD), engine disposed;
  * flag ON + isolating role -> returns cleanly, engine disposed.

It asserts the ORG-BOUND engine (``build_rls_engine(settings.database_url)``) — the MAINTENANCE
(owner) engine is intended to bypass RLS for the cross-org sweeps and is deliberately not asserted.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from oraclous_execution_engine_service.core.rls import RlsBypassingRoleError
from oraclous_execution_engine_service.tasks import celery_app as celery_mod
from oraclous_execution_engine_service.tasks.celery_app import (
    _assert_runtime_role_once_per_worker,
)

pytestmark = pytest.mark.unit


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    assert_on: bool,
    assertion: Any,
) -> MagicMock:
    """Patch settings + the org-bound engine factory + the role assertion. Returns the fake engine.

    settings carries the org-bound ``database_url`` (the role asserted); build_rls_engine is patched
    so no real connection opens.
    """
    monkeypatch.setattr(
        celery_mod,
        "get_settings",
        lambda: SimpleNamespace(
            rls_assert_runtime_role=assert_on,
            database_url="postgresql+asyncpg://oraclous_app:app@localhost/oraclous",
        ),
    )
    engine = MagicMock(name="engine", dispose=AsyncMock())
    monkeypatch.setattr(celery_mod, "build_rls_engine", lambda _dsn: engine)
    monkeypatch.setattr(celery_mod, "assert_runtime_role_isolates", assertion)
    return engine


def test_flag_off_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (owner DSN dev/test): no engine built, no assertion, worker boots normally."""
    built = MagicMock(side_effect=AssertionError("engine must NOT be built when the flag is off"))
    monkeypatch.setattr(
        celery_mod,
        "get_settings",
        lambda: SimpleNamespace(
            rls_assert_runtime_role=False,
            database_url="postgresql+asyncpg://oraclous:oraclous@localhost/oraclous",
        ),
    )
    monkeypatch.setattr(celery_mod, "build_rls_engine", built)

    _assert_runtime_role_once_per_worker()  # must not raise, must not build an engine

    built.assert_not_called()


def test_flag_on_bypassing_role_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker whose org-bound DSN is mis-deployed on a bypassing (owner) role must refuse to come
    up — SystemExit."""
    engine = _patch(
        monkeypatch,
        assert_on=True,
        assertion=AsyncMock(side_effect=RlsBypassingRoleError("rolsuper=True")),
    )

    with pytest.raises(SystemExit) as exc_info:
        _assert_runtime_role_once_per_worker()

    assert exc_info.value.code == 1
    engine.dispose.assert_awaited_once()  # disposed even on the failure path


def test_flag_on_isolating_role_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployed NOSUPERUSER oraclous_app org-bound role passes the assertion; worker boots."""
    engine = _patch(monkeypatch, assert_on=True, assertion=AsyncMock(return_value=None))

    _assert_runtime_role_once_per_worker()  # must not raise

    engine.dispose.assert_awaited_once()
