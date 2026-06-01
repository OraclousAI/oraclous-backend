"""
Unit tests for ORA-218 — lifespan must initialize both async and sync Neo4j drivers.

Regression guard: connect_sync() must be called during startup so that
sync-driver-dependent endpoints are available immediately on container start
rather than returning 503 until the first Celery job fires.
"""

import ast
import inspect
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lifespan_calls_connect_sync(monkeypatch, request):
    """connect_sync() must be called during lifespan startup (ORA-218 regression)."""
    # Stub modules that are unavailable in unit-test context.
    # Use monkeypatch.setitem so sys.modules is restored after test teardown,
    # preventing these stubs from polluting other tests (e.g. test_rate_limiting).
    for mod_name in [
        "app.api.v1.router",
        "app.core.rate_limiter",
        "app.services.memory_service",
        "app.services.database_connector_service",
    ]:
        stub = types.ModuleType(mod_name)
        if mod_name == "app.api.v1.router":
            stub.api_router = MagicMock()
        elif mod_name == "app.core.rate_limiter":
            stub.limiter = MagicMock()
        elif mod_name == "app.services.memory_service":
            stub.ensure_memory_indexes = AsyncMock()
        elif mod_name == "app.services.database_connector_service":
            mock_db_connector = MagicMock()
            mock_db_connector.ensure_constraints = AsyncMock()
            stub.database_connector_service = mock_db_connector
        monkeypatch.setitem(sys.modules, mod_name, stub)

    # Stub slowapi only when not already installed.
    if "slowapi" not in sys.modules:
        slowapi_stub = types.ModuleType("slowapi")
        slowapi_stub.RateLimitExceeded = type("RateLimitExceeded", (Exception,), {})
        slowapi_stub._rate_limit_exceeded_handler = MagicMock()
        monkeypatch.setitem(sys.modules, "slowapi", slowapi_stub)
    if "slowapi.errors" not in sys.modules:
        slowapi_errors_stub = types.ModuleType("slowapi.errors")
        slowapi_errors_stub.RateLimitExceeded = sys.modules["slowapi"].RateLimitExceeded
        monkeypatch.setitem(sys.modules, "slowapi.errors", slowapi_errors_stub)

    # Ensure app.main is re-imported fresh (cleared via monkeypatch for auto-restore).
    # monkeypatch.delitem only records a restore when the key was already present;
    # if app.main was absent the stub-backed import that follows would escape teardown
    # and poison sys.modules for subsequent tests (e.g. test_main_app_has_limiter_attached).
    # The request finalizer removes it unconditionally after the test.
    monkeypatch.delitem(sys.modules, "app.main", raising=False)
    request.addfinalizer(lambda: sys.modules.pop("app.main", None))

    # Build mock neo4j_client
    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.connect_sync = MagicMock()
    mock_client.disconnect = AsyncMock()
    mock_client.async_driver = MagicMock()

    # Service mocks
    mock_rebac = MagicMock()
    mock_rebac.initialize_schema = AsyncMock()
    mock_rebac.initialize_schema_full = AsyncMock()
    mock_rebac.seed_system_permissions = AsyncMock()
    mock_rebac.sync_existing_data = AsyncMock()

    mock_snapshot = MagicMock()
    mock_snapshot.ensure_indexes = AsyncMock()

    mock_sa_service = MagicMock()
    mock_sa_service.initialize_schema = AsyncMock()

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.core.neo4j_client.neo4j_client", mock_client),
        patch("app.core.database.init_database_schema", new_callable=AsyncMock),
        patch("app.core.database.async_session_maker", return_value=mock_session),
        patch("app.core.telemetry.setup_telemetry"),
        patch("app.core.telemetry.shutdown_telemetry"),
        patch("app.core.telemetry.instrument_fastapi"),
    ):
        import importlib

        import app.main as main_module

        importlib.reload(main_module)
        lifespan_fn = main_module.lifespan
        app_obj = main_module.app

        with (
            patch("app.services.rebac_service.rebac_service", mock_rebac, create=True),
            patch("app.core.database.async_session_maker", return_value=mock_session),
            patch(
                "app.services.snapshot_service.snapshot_service",
                mock_snapshot,
                create=True,
            ),
            patch(
                "app.services.pipeline_service.ensure_fingerprint_indexes",
                AsyncMock(),
                create=True,
            ),
            patch(
                "app.services.code_parser_service.ensure_code_schema",
                AsyncMock(),
                create=True,
            ),
            patch(
                "app.services.service_account_service.service_account_service",
                mock_sa_service,
                create=True,
            ),
            patch(
                "app.services.memory_service.ensure_memory_indexes",
                AsyncMock(),
                create=True,
            ),
            patch.object(main_module, "neo4j_client", mock_client),
        ):
            async with lifespan_fn(app_obj):
                pass

    mock_client.connect.assert_awaited_once()
    mock_client.connect_sync.assert_called_once()


@pytest.mark.unit
def test_main_py_calls_connect_sync_after_connect():
    """Static AST guard: lifespan source must call connect_sync() after connect()."""
    import app.main as main_module

    source = inspect.getsource(main_module.lifespan)
    tree = ast.parse(source)

    # Collect (lineno, method_name) in source order
    calls = sorted(
        (node.lineno, node.func.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    )
    call_names = [name for _, name in calls]

    assert "connect" in call_names, "lifespan must call neo4j_client.connect()"
    assert "connect_sync" in call_names, (
        "lifespan must call neo4j_client.connect_sync() — ORA-218 regression guard"
    )

    connect_idx = call_names.index("connect")
    connect_sync_idx = call_names.index("connect_sync")
    assert connect_idx < connect_sync_idx, "connect_sync() must be called after connect()"
