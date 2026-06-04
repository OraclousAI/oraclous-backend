"""Retrieval module extraction tests (ORAA-56).

Acceptance criterion 2: the five core retrieval modules live *only* in
``oraclous_knowledge_retriever_service``.

Modules under test (extracted from knowledge-graph-builder):
  - retriever_service       (RetrieverService — orchestrates retrieval requests)
  - retriever_factory       (RetrieverFactory — instantiates retriever combinations)
  - query_cache_service     (QueryCacheService — deduplicates in-flight and
                             recent identical queries)
  - fulltext_index_service  (FulltextIndexService — manages fulltext index
                             lifecycle for a given graph)
  - similarity_service      (SimilarityService — vector similarity primitives)

Imports of the not-yet-built seams are function-local per ORA-48 / TST001:
collection succeeds; each test fails RED at runtime with ``ModuleNotFoundError``
(or ``ImportError``) until the ``[impl]`` PR lands the modules.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Module importability
# ---------------------------------------------------------------------------


class TestRetrieverServiceModuleImport:
    """``oraclous_knowledge_retriever_service.retriever_service`` is importable."""

    def test_retriever_service_module_is_importable(self) -> None:
        """The retriever_service module must be importable from the KRS package."""
        from oraclous_knowledge_retriever_service import (  # ORA-48
            retriever_service,
        )

        assert retriever_service is not None

    def test_retriever_service_exposes_retriever_service_class(self) -> None:
        """``retriever_service`` module must expose a ``RetrieverService`` class."""
        from oraclous_knowledge_retriever_service.retriever_service import (  # ORA-48
            RetrieverService,
        )

        assert callable(RetrieverService)

    def test_retriever_service_has_retrieve_method(self) -> None:
        """``RetrieverService`` must expose a ``retrieve`` callable."""
        from oraclous_knowledge_retriever_service.retriever_service import (  # ORA-48
            RetrieverService,
        )

        assert hasattr(RetrieverService, "retrieve") or any(
            "retrieve" in name for name in dir(RetrieverService)
        )


class TestRetrieverFactoryModuleImport:
    """``oraclous_knowledge_retriever_service.retriever_factory`` is importable."""

    def test_retriever_factory_module_is_importable(self) -> None:
        """The retriever_factory module must be importable from the KRS package."""
        from oraclous_knowledge_retriever_service import (  # ORA-48
            retriever_factory,
        )

        assert retriever_factory is not None

    def test_retriever_factory_exposes_retriever_factory_class(self) -> None:
        """``retriever_factory`` module must expose a ``RetrieverFactory`` class."""
        from oraclous_knowledge_retriever_service.retriever_factory import (  # ORA-48
            RetrieverFactory,
        )

        assert callable(RetrieverFactory)

    def test_retriever_factory_has_create_method(self) -> None:
        """``RetrieverFactory`` must expose a ``create`` classmethod or staticmethod."""
        from oraclous_knowledge_retriever_service.retriever_factory import (  # ORA-48
            RetrieverFactory,
        )

        assert hasattr(RetrieverFactory, "create")


class TestQueryCacheServiceModuleImport:
    """``oraclous_knowledge_retriever_service.query_cache_service`` is importable."""

    def test_query_cache_service_module_is_importable(self) -> None:
        """The query_cache_service module must be importable from the KRS package."""
        from oraclous_knowledge_retriever_service import (  # ORA-48
            query_cache_service,
        )

        assert query_cache_service is not None

    def test_query_cache_service_exposes_query_cache_service_class(self) -> None:
        """``query_cache_service`` module must expose a ``QueryCacheService`` class."""
        from oraclous_knowledge_retriever_service.query_cache_service import (  # ORA-48
            QueryCacheService,
        )

        assert callable(QueryCacheService)


class TestFulltextIndexServiceModuleImport:
    """``oraclous_knowledge_retriever_service.fulltext_index_service`` is importable."""

    def test_fulltext_index_service_module_is_importable(self) -> None:
        """The fulltext_index_service module must be importable from the KRS package."""
        from oraclous_knowledge_retriever_service import (  # ORA-48
            fulltext_index_service,
        )

        assert fulltext_index_service is not None

    def test_fulltext_index_service_exposes_fulltext_index_service_class(self) -> None:
        """``fulltext_index_service`` module must expose a ``FulltextIndexService`` class."""
        from oraclous_knowledge_retriever_service.fulltext_index_service import (  # ORA-48
            FulltextIndexService,
        )

        assert callable(FulltextIndexService)


class TestSimilarityServiceModuleImport:
    """``oraclous_knowledge_retriever_service.similarity_service`` is importable."""

    def test_similarity_service_module_is_importable(self) -> None:
        """The similarity_service module must be importable from the KRS package."""
        from oraclous_knowledge_retriever_service import (  # ORA-48
            similarity_service,
        )

        assert similarity_service is not None

    def test_similarity_service_exposes_similarity_service_class(self) -> None:
        """``similarity_service`` module must expose a ``SimilarityService`` class."""
        from oraclous_knowledge_retriever_service.similarity_service import (  # ORA-48
            SimilarityService,
        )

        assert callable(SimilarityService)


# ---------------------------------------------------------------------------
# All five modules registered in the package namespace
# ---------------------------------------------------------------------------


class TestAllFiveModulesPresent:
    """All five extraction targets must be reachable from the package root."""

    _EXPECTED_MODULES = [
        "retriever_service",
        "retriever_factory",
        "query_cache_service",
        "fulltext_index_service",
        "similarity_service",
    ]

    @pytest.mark.parametrize("module_name", _EXPECTED_MODULES)
    def test_module_is_importable_from_krs_package(self, module_name: str) -> None:
        """Each extraction target must live inside ``oraclous_knowledge_retriever_service``."""
        import importlib

        # ORA-48: import deferred into the test body
        mod = importlib.import_module(f"oraclous_knowledge_retriever_service.{module_name}")
        assert mod is not None, f"Module {module_name!r} missing from KRS package"
