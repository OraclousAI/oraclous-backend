"""KGS deprecation shim tests (ORAA-56).

Acceptance criterion 3: the deprecation shim in knowledge-graph-service logs
every missed retrieval call.

When any of the five retrieval modules is invoked through the KGS shim
(i.e. the caller has not yet updated their import to use KRS directly):

1. A WARNING-level log message is emitted that names the deprecated KGS path
   and the canonical KRS replacement.
2. The call is transparently forwarded to the KRS implementation so existing
   callers are not broken before they migrate.

Imports of the not-yet-built shim ``oraclous_knowledge_graph_service.*``
delegation paths are function-local per ORA-48 / TST001 — collection succeeds;
each test fails RED at runtime with ``ModuleNotFoundError`` (or ``ImportError``)
until the ``[impl]`` PR lands the shim.

The shim modules expected in KGS are:
  - ``oraclous_knowledge_graph_service.retriever_service`` (shim)
  - ``oraclous_knowledge_graph_service.retriever_factory`` (shim)
  - ``oraclous_knowledge_graph_service.query_cache_service`` (shim)
  - ``oraclous_knowledge_graph_service.fulltext_index_service`` (shim)
  - ``oraclous_knowledge_graph_service.similarity_service`` (shim)
"""

from __future__ import annotations

import logging

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_SHIM_MODULES = [
    "retriever_service",
    "retriever_factory",
    "query_cache_service",
    "fulltext_index_service",
    "similarity_service",
]


# ---------------------------------------------------------------------------
# Shim importability
# ---------------------------------------------------------------------------


class TestKGSShimModulesAreImportable:
    """Each shim must be importable from ``oraclous_knowledge_graph_service``."""

    @pytest.mark.parametrize("module_name", _SHIM_MODULES)
    def test_shim_module_importable_from_kgs(self, module_name: str) -> None:
        """KGS shim module must be importable — it is the backwards-compat surface."""
        import importlib

        # ORA-48: deferred import — fails RED until impl
        mod = importlib.import_module(f"oraclous_knowledge_graph_service.{module_name}")
        assert mod is not None, (
            f"KGS shim {module_name!r} not found; "
            "the [impl] PR must create deprecation shims in KGS"
        )


# ---------------------------------------------------------------------------
# Deprecation log on import
# ---------------------------------------------------------------------------


class TestKGSShimEmitsDeprecationLogOnImport:
    """Importing a retrieval module via the KGS shim path must emit a WARNING.

    The log must identify the deprecated KGS path and name the KRS
    replacement so consumers can migrate with minimal friction.
    """

    @pytest.mark.parametrize("module_name", _SHIM_MODULES)
    def test_importing_shim_emits_warning_level_log(
        self, module_name: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Importing the KGS shim emits at least one WARNING about deprecation."""
        import importlib
        import sys

        kgs_module_path = f"oraclous_knowledge_graph_service.{module_name}"

        # Evict from sys.modules so the import fires fresh in this test.
        sys.modules.pop(kgs_module_path, None)

        with caplog.at_level(logging.WARNING, logger="oraclous_knowledge_graph_service"):
            importlib.import_module(kgs_module_path)  # ORA-48 — RED until impl

        deprecation_records = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "deprecat" in r.getMessage().lower()
        ]
        assert deprecation_records, (
            f"Importing KGS shim {module_name!r} must emit a deprecation WARNING; "
            "none was found in the captured log"
        )

    @pytest.mark.parametrize("module_name", _SHIM_MODULES)
    def test_deprecation_log_names_krs_replacement(
        self, module_name: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The deprecation log must name the KRS canonical replacement path."""
        import importlib
        import sys

        kgs_module_path = f"oraclous_knowledge_graph_service.{module_name}"
        sys.modules.pop(kgs_module_path, None)

        with caplog.at_level(logging.WARNING, logger="oraclous_knowledge_graph_service"):
            importlib.import_module(kgs_module_path)  # ORA-48 — RED until impl

        combined = " ".join(r.getMessage() for r in caplog.records)
        assert "oraclous_knowledge_retriever_service" in combined, (
            f"Deprecation log for {module_name!r} must mention the KRS replacement "
            "(oraclous_knowledge_retriever_service) so callers know where to migrate"
        )


# ---------------------------------------------------------------------------
# Call-time deprecation log (shim forwards but logs on every call)
# ---------------------------------------------------------------------------


class TestKGSShimLogsOnEveryRetrievalCall:
    """Each call through the KGS shim must emit a WARNING — not only at import time.

    The acceptance criterion says "logs every missed retrieval call", so the
    shim must log per-call, not only when the module is first loaded.
    This test doubles the underlying KRS implementation to prevent real I/O.
    """

    def test_retriever_service_shim_logs_on_each_retrieve_call(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Calling ``RetrieverService.retrieve`` via the KGS shim logs a WARNING."""
        from oraclous_knowledge_graph_service import (  # ORA-48 — RED until impl
            retriever_service as kgs_shim,
        )

        # Replace the KRS delegate so the call doesn't do real work.
        class _FakeRetrieverService:
            def retrieve(self, *args, **kwargs):
                return []

        monkeypatch.setattr(
            kgs_shim,
            "_krs_retriever_service",
            _FakeRetrieverService(),
            raising=False,
        )

        service = kgs_shim.RetrieverService()

        with caplog.at_level(logging.WARNING, logger="oraclous_knowledge_graph_service"):
            service.retrieve(query="test")
            service.retrieve(query="test-again")

        warning_calls = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "deprecat" in r.getMessage().lower()
        ]
        assert len(warning_calls) >= 2, (
            "RetrieverService.retrieve via KGS shim must log a deprecation WARNING "
            "on *every* call, not only the first"
        )

    def test_query_cache_service_shim_logs_on_each_call(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Calling any method of ``QueryCacheService`` via the KGS shim logs a WARNING."""
        from oraclous_knowledge_graph_service import (  # ORA-48 — RED until impl
            query_cache_service as kgs_shim,
        )

        class _FakeQueryCacheService:
            def get(self, *args, **kwargs):
                return None

            def set(self, *args, **kwargs):
                pass

        monkeypatch.setattr(
            kgs_shim,
            "_krs_query_cache_service",
            _FakeQueryCacheService(),
            raising=False,
        )

        svc = kgs_shim.QueryCacheService()

        with caplog.at_level(logging.WARNING, logger="oraclous_knowledge_graph_service"):
            svc.get(key="k1")
            svc.get(key="k2")

        warning_calls = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "deprecat" in r.getMessage().lower()
        ]
        assert len(warning_calls) >= 2, (
            "QueryCacheService.get via KGS shim must log a deprecation WARNING on every call"
        )
