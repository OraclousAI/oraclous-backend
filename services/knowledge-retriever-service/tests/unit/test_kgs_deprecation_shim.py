"""KGS deprecation shim tests — HTTP shim (ADR-014 Option B), ORAA-56.

Acceptance criterion 3: the deprecation shim in knowledge-graph-service logs
every missed retrieval call AND forwards it to KRS via HTTP.

ADR-014 Decision (2026-06-04): Option B — KGS calls KRS REST API as shim.
KGS and KRS run in separate Docker containers; Python library-import shims
cannot cross process boundaries.  The KGS shim must:

1. Emit a DeprecationWarning-level log *before* delegating.
2. Call the canonical KRS REST endpoint via ``httpx.AsyncClient``.
3. Return the KRS response body to the caller unchanged.
4. Read ``KRS_BASE_URL`` from env (default: ``http://krs-service:8006``).

Tests mock ``httpx.AsyncClient`` at the transport layer using
``_CaptureTransport`` (an ``httpx.AsyncBaseTransport`` subclass).  No
Python-attribute patching of KRS modules.  The shim modules expose a
module-level ``_transport`` variable (default ``None`` → real network) that
tests inject without replacing the entire client.

All imports of the not-yet-built shim modules are function-local per
ORA-48 / TST001 — ``pytest --collect-only`` succeeds; each test fails RED
with ``ModuleNotFoundError`` until the ``[impl]`` PR creates the HTTP shim
modules in KGS.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Transport-layer test double
# ---------------------------------------------------------------------------


class _CaptureTransport(httpx.AsyncBaseTransport):
    """Records outbound httpx requests; returns a canned JSON response."""

    def __init__(self, status_code: int = 200, json_body: Any = None) -> None:
        self.requests: list[httpx.Request] = []
        self._status = status_code
        self._json_body = json_body if json_body is not None else {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            self._status,
            content=json.dumps(self._json_body).encode(),
            headers={"content-type": "application/json"},
            request=request,
        )


@pytest.fixture
def capture_transport() -> _CaptureTransport:
    return _CaptureTransport(json_body={"results": []})


# ---------------------------------------------------------------------------
# Module list
# ---------------------------------------------------------------------------

_SHIM_MODULES = [
    "retriever_service",
    "retriever_factory",
    "query_cache_service",
    "fulltext_index_service",
    "similarity_service",
]


# ---------------------------------------------------------------------------
# AC3.1 — Shim modules are importable from KGS
# ---------------------------------------------------------------------------


class TestKGSShimModulesAreImportable:
    """Each HTTP shim must be importable from ``oraclous_knowledge_graph_service``."""

    @pytest.mark.parametrize("module_name", _SHIM_MODULES)
    def test_shim_module_importable_from_kgs(self, module_name: str) -> None:
        """KGS shim module must be importable — it is the backwards-compat surface."""
        import importlib  # ORA-48: deferred import — fails RED until impl

        mod = importlib.import_module(f"oraclous_knowledge_graph_service.{module_name}")
        assert mod is not None, (
            f"KGS HTTP shim {module_name!r} not found; "
            "the [impl] PR must create HTTP deprecation shims in KGS"
        )


# ---------------------------------------------------------------------------
# AC3.2 — Shim emits DeprecationWarning before delegating
# ---------------------------------------------------------------------------


class TestKGSShimEmitsDeprecationLogBeforeHTTPCall:
    """Each shim call must emit a DeprecationWarning log before the HTTP call.

    The log must name the deprecated KGS path and the canonical KRS replacement
    so consumers can migrate with minimal friction.
    """

    async def test_retriever_service_logs_deprecation_on_retrieve(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        capture_transport: _CaptureTransport,
    ) -> None:
        """RetrieverService.retrieve logs DeprecationWarning before HTTP call to KRS."""
        from oraclous_knowledge_graph_service import (  # ORA-48 — RED until impl
            retriever_service as shim,
        )

        monkeypatch.setenv("KRS_BASE_URL", "http://krs-test:8006")
        monkeypatch.setattr(shim, "_transport", capture_transport, raising=False)

        svc = shim.RetrieverService()
        with caplog.at_level(logging.WARNING, logger="oraclous_knowledge_graph_service"):
            await svc.retrieve(query="test-query")

        deprecation_records = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "deprecat" in r.getMessage().lower()
        ]
        assert deprecation_records, (
            "RetrieverService.retrieve via KGS shim must emit a DeprecationWarning log; "
            "none was found"
        )

    async def test_deprecation_log_names_krs_replacement_package(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        capture_transport: _CaptureTransport,
    ) -> None:
        """The deprecation log must name oraclous_knowledge_retriever_service."""
        from oraclous_knowledge_graph_service import (  # ORA-48 — RED until impl
            retriever_service as shim,
        )

        monkeypatch.setenv("KRS_BASE_URL", "http://krs-test:8006")
        monkeypatch.setattr(shim, "_transport", capture_transport, raising=False)

        svc = shim.RetrieverService()
        with caplog.at_level(logging.WARNING, logger="oraclous_knowledge_graph_service"):
            await svc.retrieve(query="test")

        combined = " ".join(r.getMessage() for r in caplog.records)
        assert "oraclous_knowledge_retriever_service" in combined, (
            "Deprecation log must name the KRS replacement package "
            "(oraclous_knowledge_retriever_service) so callers know where to migrate"
        )

    async def test_query_cache_service_logs_deprecation_on_get(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        capture_transport: _CaptureTransport,
    ) -> None:
        """QueryCacheService.get logs DeprecationWarning before HTTP call to KRS."""
        from oraclous_knowledge_graph_service import (  # ORA-48 — RED until impl
            query_cache_service as shim,
        )

        monkeypatch.setenv("KRS_BASE_URL", "http://krs-test:8006")
        monkeypatch.setattr(shim, "_transport", capture_transport, raising=False)

        svc = shim.QueryCacheService()
        with caplog.at_level(logging.WARNING, logger="oraclous_knowledge_graph_service"):
            await svc.get(key="cache-key-1")

        deprecation_records = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "deprecat" in r.getMessage().lower()
        ]
        assert deprecation_records, (
            "QueryCacheService.get via KGS shim must emit a DeprecationWarning log"
        )


# ---------------------------------------------------------------------------
# AC3.3 — Shim calls KRS REST endpoint via httpx.AsyncClient
# ---------------------------------------------------------------------------


class TestKGSShimCallsKRSEndpointViaHTTP:
    """The shim must call KRS REST endpoint via httpx.AsyncClient.

    Tests inject _CaptureTransport into the shim via the module-level
    ``_transport`` attribute.  After the call, ``_CaptureTransport.requests``
    is inspected to assert the correct URL and method were used.
    """

    async def test_retriever_service_makes_http_call_to_krs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture_transport: _CaptureTransport,
    ) -> None:
        """RetrieverService.retrieve makes at least one HTTP call to KRS_BASE_URL."""
        from oraclous_knowledge_graph_service import (  # ORA-48 — RED until impl
            retriever_service as shim,
        )

        monkeypatch.setenv("KRS_BASE_URL", "http://krs-test:8006")
        monkeypatch.setattr(shim, "_transport", capture_transport, raising=False)

        svc = shim.RetrieverService()
        await svc.retrieve(query="hello")

        assert capture_transport.requests, (
            "RetrieverService.retrieve must make at least one HTTP call to KRS; none were made"
        )
        outbound = capture_transport.requests[0]
        assert "krs-test:8006" in str(outbound.url), (
            f"HTTP call must target KRS_BASE_URL (krs-test:8006); got {outbound.url!r}"
        )

    async def test_krs_base_url_default_is_krs_service_8006(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture_transport: _CaptureTransport,
    ) -> None:
        """When KRS_BASE_URL is unset, the shim defaults to http://krs-service:8006."""
        from oraclous_knowledge_graph_service import (  # ORA-48 — RED until impl
            retriever_service as shim,
        )

        monkeypatch.delenv("KRS_BASE_URL", raising=False)
        monkeypatch.setattr(shim, "_transport", capture_transport, raising=False)

        svc = shim.RetrieverService()
        await svc.retrieve(query="default-url-test")

        assert capture_transport.requests, (
            "RetrieverService.retrieve must make an HTTP call even when KRS_BASE_URL is unset"
        )
        outbound = capture_transport.requests[0]
        assert "krs-service:8006" in str(outbound.url), (
            f"Default KRS_BASE_URL must be http://krs-service:8006; got {outbound.url!r}"
        )

    async def test_retriever_factory_makes_http_call_to_krs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture_transport: _CaptureTransport,
    ) -> None:
        """RetrieverFactory.create makes an HTTP call to the KRS factory endpoint."""
        from oraclous_knowledge_graph_service import (  # ORA-48 — RED until impl
            retriever_factory as shim,
        )

        monkeypatch.setenv("KRS_BASE_URL", "http://krs-test:8006")
        monkeypatch.setattr(shim, "_transport", capture_transport, raising=False)

        factory = shim.RetrieverFactory()
        await factory.create(retriever_type="vector")

        assert capture_transport.requests, (
            "RetrieverFactory.create must make at least one HTTP call to KRS"
        )
        assert "krs-test:8006" in str(capture_transport.requests[0].url), (
            "HTTP call must target KRS_BASE_URL"
        )

    async def test_similarity_service_makes_http_call_to_krs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture_transport: _CaptureTransport,
    ) -> None:
        """SimilarityService makes an HTTP call to the KRS similarity endpoint."""
        from oraclous_knowledge_graph_service import (  # ORA-48 — RED until impl
            similarity_service as shim,
        )

        monkeypatch.setenv("KRS_BASE_URL", "http://krs-test:8006")
        monkeypatch.setattr(shim, "_transport", capture_transport, raising=False)

        svc = shim.SimilarityService()
        await svc.compute(node_id="node-1")

        assert capture_transport.requests, (
            "SimilarityService.compute must make at least one HTTP call to KRS"
        )
        assert "krs-test:8006" in str(capture_transport.requests[0].url)


# ---------------------------------------------------------------------------
# AC3.4 — Shim returns KRS response body to caller
# ---------------------------------------------------------------------------


class TestKGSShimReturnsKRSResponse:
    """The shim must pass the KRS HTTP response body back to the caller unchanged."""

    async def test_retriever_service_returns_krs_response_body(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RetrieverService.retrieve returns the KRS JSON response body."""
        from oraclous_knowledge_graph_service import (  # ORA-48 — RED until impl
            retriever_service as shim,
        )

        expected = {"results": [{"id": "node-1", "score": 0.97}]}
        transport = _CaptureTransport(status_code=200, json_body=expected)

        monkeypatch.setenv("KRS_BASE_URL", "http://krs-test:8006")
        monkeypatch.setattr(shim, "_transport", transport, raising=False)

        svc = shim.RetrieverService()
        result = await svc.retrieve(query="test-passthrough")

        assert result == expected, f"Shim must return KRS response body unchanged; got {result!r}"

    async def test_query_cache_service_returns_krs_response_body(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """QueryCacheService.get returns the KRS cache response body."""
        from oraclous_knowledge_graph_service import (  # ORA-48 — RED until impl
            query_cache_service as shim,
        )

        expected = {"cached": True, "value": {"nodes": []}}
        transport = _CaptureTransport(status_code=200, json_body=expected)

        monkeypatch.setenv("KRS_BASE_URL", "http://krs-test:8006")
        monkeypatch.setattr(shim, "_transport", transport, raising=False)

        svc = shim.QueryCacheService()
        result = await svc.get(key="cache-key-1")

        assert result == expected, (
            f"KGS cache shim must pass KRS response back to caller; got {result!r}"
        )


# ---------------------------------------------------------------------------
# AC3.5 — Shim logs a DeprecationWarning on EVERY call, not only the first
# ---------------------------------------------------------------------------


class TestKGSShimLogsOnEveryCall:
    """Each call through the KGS shim must emit a DeprecationWarning.

    AC3 states 'logs every missed retrieval call' — per-call, not per-import.
    """

    async def test_retriever_service_logs_on_each_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        capture_transport: _CaptureTransport,
    ) -> None:
        """RetrieverService.retrieve emits DeprecationWarning on each invocation."""
        from oraclous_knowledge_graph_service import (  # ORA-48 — RED until impl
            retriever_service as shim,
        )

        monkeypatch.setenv("KRS_BASE_URL", "http://krs-test:8006")
        monkeypatch.setattr(shim, "_transport", capture_transport, raising=False)

        svc = shim.RetrieverService()
        with caplog.at_level(logging.WARNING, logger="oraclous_knowledge_graph_service"):
            await svc.retrieve(query="call-1")
            await svc.retrieve(query="call-2")

        warning_calls = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "deprecat" in r.getMessage().lower()
        ]
        assert len(warning_calls) >= 2, (
            "RetrieverService.retrieve must log DeprecationWarning on *every* call; "
            f"only {len(warning_calls)} warning(s) captured for 2 calls"
        )

    async def test_query_cache_service_logs_on_each_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        capture_transport: _CaptureTransport,
    ) -> None:
        """QueryCacheService.get emits DeprecationWarning on each invocation."""
        from oraclous_knowledge_graph_service import (  # ORA-48 — RED until impl
            query_cache_service as shim,
        )

        monkeypatch.setenv("KRS_BASE_URL", "http://krs-test:8006")
        monkeypatch.setattr(shim, "_transport", capture_transport, raising=False)

        svc = shim.QueryCacheService()
        with caplog.at_level(logging.WARNING, logger="oraclous_knowledge_graph_service"):
            await svc.get(key="k1")
            await svc.get(key="k2")

        warning_calls = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "deprecat" in r.getMessage().lower()
        ]
        assert len(warning_calls) >= 2, (
            "QueryCacheService.get via KGS shim must log DeprecationWarning on every call; "
            f"only {len(warning_calls)} warning(s) captured for 2 calls"
        )
