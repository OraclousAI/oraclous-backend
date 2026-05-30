"""Multi-tenant retriever wrappers — preserved legacy ``graph_id`` behaviour
(ORA-18 / Epic A3, Lift step).

Behavioural reference: legacy
``knowledge-graph-builder/app/components/multi_tenant_components.py``
(``MultiTenantRetriever``, ``MultiTenantVectorRetriever``,
``MultiTenantVectorCypherRetriever``, ``MultiTenantHybridRetriever``). These
tests pin the legacy ``graph_id`` injection contract so the lift cannot
regress it; the *outer organisation-scoping layer* added by ORA-18 is covered
in [test_organisation_scoping_layer.py](./test_organisation_scoping_layer.py).

Imports of the not-yet-built seam ``oraclous_knowledge_retriever_service.
multi_tenant`` are function-local per ORA-48 / TST001 (the TDD-window
collection-safety convention): collection succeeds, each test fails RED at
*runtime* with ``ModuleNotFoundError`` until backend-implementer lands the
``[impl]`` PR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeItem:
    """Stand-in for ``neo4j_graphrag.types.RetrieverResultItem``."""

    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeResult:
    items: list[_FakeItem]


class _FakeBaseRetriever:
    """Records ``get_search_results`` kwargs and returns a configured result.

    Stands in for any ``neo4j_graphrag.retrievers.Retriever`` instance so the
    wrapper can be exercised without a live Neo4j driver or vector index.
    """

    def __init__(
        self,
        *,
        items: list[_FakeItem] | None = None,
        driver: object | None = None,
    ) -> None:
        self.driver = driver if driver is not None else object()
        self.calls: list[dict[str, Any]] = []
        self._items = items or []
        self.embedder = object()
        self.index_name = "captured-base-index"

    def get_search_results(
        self,
        query_vector: list[float] | None = None,
        query_text: str | None = None,
        **kwargs: Any,
    ) -> _FakeResult:
        self.calls.append({"query_vector": query_vector, "query_text": query_text, **kwargs})
        return _FakeResult(items=list(self._items))


# ---------------------------------------------------------------------------
# Tenant-specific index naming
# ---------------------------------------------------------------------------


class TestTenantIndexNaming:
    """Index names are made tenant-specific by suffixing ``_<graph_id>``.

    Legacy reference: ``MultiTenantVectorRetriever.create`` L118 and
    ``MultiTenantHybridRetriever.create`` L228.
    """

    def test_vector_retriever_creates_tenant_specific_index_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from oraclous_knowledge_retriever_service.multi_tenant import (
            MultiTenantVectorRetriever,
        )

        captured: dict[str, Any] = {}

        def _fake_base_vector_retriever(**kwargs: Any) -> _FakeBaseRetriever:
            captured.update(kwargs)
            return _FakeBaseRetriever()

        monkeypatch.setattr(
            "oraclous_knowledge_retriever_service.multi_tenant.VectorRetriever",
            _fake_base_vector_retriever,
        )

        MultiTenantVectorRetriever.create(
            driver=object(),
            index_name="entity_embeddings",
            embedder=object(),
            graph_id="graph-xyz",
        )

        assert captured["index_name"] == "entity_embeddings_graph-xyz"

    def test_hybrid_retriever_creates_tenant_specific_vector_and_fulltext_indices(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from oraclous_knowledge_retriever_service.multi_tenant import (
            MultiTenantHybridRetriever,
        )

        captured: dict[str, Any] = {}

        def _fake_base_hybrid_retriever(**kwargs: Any) -> _FakeBaseRetriever:
            captured.update(kwargs)
            return _FakeBaseRetriever()

        monkeypatch.setattr(
            "oraclous_knowledge_retriever_service.multi_tenant.HybridRetriever",
            _fake_base_hybrid_retriever,
        )

        MultiTenantHybridRetriever.create(
            driver=object(),
            vector_index_name="entity_embeddings",
            fulltext_index_name="entity_text_fulltext",
            embedder=object(),
            graph_id="graph-xyz",
        )

        assert captured["vector_index_name"] == "entity_embeddings_graph-xyz"
        assert captured["fulltext_index_name"] == "entity_text_fulltext_graph-xyz"


# ---------------------------------------------------------------------------
# graph_id injection into filters and query_params
# ---------------------------------------------------------------------------


class TestGraphIdInjection:
    """Every search call has ``graph_id`` injected into both index ``filters``
    and Cypher ``query_params`` before delegating to the base retriever.

    Legacy reference: ``MultiTenantRetriever.get_search_results`` L52-60.
    """

    def test_search_injects_graph_id_into_filters(self) -> None:
        from oraclous_knowledge_retriever_service.multi_tenant import (
            MultiTenantVectorRetriever,
        )

        base = _FakeBaseRetriever()
        wrapper = MultiTenantVectorRetriever(base_retriever=base, graph_id="graph-A")

        wrapper.get_search_results(query_text="hello")

        assert base.calls[0]["filters"] == {"graph_id": "graph-A"}

    def test_search_injects_graph_id_into_query_params(self) -> None:
        from oraclous_knowledge_retriever_service.multi_tenant import (
            MultiTenantVectorRetriever,
        )

        base = _FakeBaseRetriever()
        wrapper = MultiTenantVectorRetriever(base_retriever=base, graph_id="graph-A")

        wrapper.get_search_results(query_text="hello")

        assert base.calls[0]["query_params"] == {"graph_id": "graph-A"}

    def test_search_preserves_caller_supplied_filter_keys(self) -> None:
        """Caller may pass other filter keys (e.g. ``score``); we add, not replace."""
        from oraclous_knowledge_retriever_service.multi_tenant import (
            MultiTenantVectorRetriever,
        )

        base = _FakeBaseRetriever()
        wrapper = MultiTenantVectorRetriever(base_retriever=base, graph_id="graph-A")

        wrapper.get_search_results(query_text="hello", filters={"min_score": 0.5})

        sent_filters = base.calls[0]["filters"]
        assert sent_filters["min_score"] == 0.5
        assert sent_filters["graph_id"] == "graph-A"


# ---------------------------------------------------------------------------
# VectorCypherRetriever: WHERE clause injection
# ---------------------------------------------------------------------------


class TestCypherWhereInjection:
    """Cypher templates get a parameterised ``$graph_id`` WHERE clause spliced
    after the first ``MATCH`` so the underlying retriever cannot return
    nodes from another tenant.

    Legacy reference: ``MultiTenantVectorCypherRetriever._inject_graph_id_filter``
    L177-198.
    """

    def test_injects_where_clause_after_first_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from oraclous_knowledge_retriever_service.multi_tenant import (
            MultiTenantVectorCypherRetriever,
        )

        captured: dict[str, Any] = {}

        def _fake_base(**kwargs: Any) -> _FakeBaseRetriever:
            captured.update(kwargs)
            return _FakeBaseRetriever()

        monkeypatch.setattr(
            "oraclous_knowledge_retriever_service.multi_tenant.VectorCypherRetriever",
            _fake_base,
        )

        MultiTenantVectorCypherRetriever.create(
            driver=object(),
            index_name="entity_embeddings",
            embedder=object(),
            retrieval_query="MATCH (node:Entity) RETURN node",
            graph_id="graph-A",
        )

        q = captured["retrieval_query"]
        assert "WHERE node.graph_id = $graph_id" in q
        assert q.index("WHERE node.graph_id = $graph_id") > q.index("MATCH")

    def test_skips_injection_when_graph_id_already_in_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A query that already names ``$graph_id`` must not be double-filtered."""
        from oraclous_knowledge_retriever_service.multi_tenant import (
            MultiTenantVectorCypherRetriever,
        )

        captured: dict[str, Any] = {}

        def _fake_base(**kwargs: Any) -> _FakeBaseRetriever:
            captured.update(kwargs)
            return _FakeBaseRetriever()

        monkeypatch.setattr(
            "oraclous_knowledge_retriever_service.multi_tenant.VectorCypherRetriever",
            _fake_base,
        )

        already = "MATCH (node:Entity {graph_id: $graph_id}) RETURN node"
        MultiTenantVectorCypherRetriever.create(
            driver=object(),
            index_name="entity_embeddings",
            embedder=object(),
            retrieval_query=already,
            graph_id="graph-A",
        )

        assert captured["retrieval_query"] == already

    def test_injects_parameterised_filter_not_string_interpolated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The graph_id value must never be interpolated into the Cypher text.

        Cypher injection vector: if the value were spliced into the query
        string a caller could escape it. Always parameterise via ``$graph_id``.
        """
        from oraclous_knowledge_retriever_service.multi_tenant import (
            MultiTenantVectorCypherRetriever,
        )

        captured: dict[str, Any] = {}

        def _fake_base(**kwargs: Any) -> _FakeBaseRetriever:
            captured.update(kwargs)
            return _FakeBaseRetriever()

        monkeypatch.setattr(
            "oraclous_knowledge_retriever_service.multi_tenant.VectorCypherRetriever",
            _fake_base,
        )

        evil = "graph-A' OR 1=1 //"
        MultiTenantVectorCypherRetriever.create(
            driver=object(),
            index_name="entity_embeddings",
            embedder=object(),
            retrieval_query="MATCH (node:Entity) RETURN node",
            graph_id=evil,
        )

        assert evil not in captured["retrieval_query"]
        assert "$graph_id" in captured["retrieval_query"]


# ---------------------------------------------------------------------------
# Post-filter back-stop
# ---------------------------------------------------------------------------


class TestPostFilterBackstop:
    """Even if the base retriever fails to honour the filter, the wrapper
    drops any result item whose ``metadata['graph_id']`` does not match.

    Legacy reference: ``MultiTenantRetriever.get_search_results`` L69-84
    ("Additional safety filtering").
    """

    def test_post_filter_keeps_items_whose_metadata_matches_graph_id(self) -> None:
        from oraclous_knowledge_retriever_service.multi_tenant import (
            MultiTenantVectorRetriever,
        )

        items = [
            _FakeItem(content="ok-1", metadata={"graph_id": "graph-A"}),
            _FakeItem(content="ok-2", metadata={"graph_id": "graph-A"}),
        ]
        base = _FakeBaseRetriever(items=items)
        wrapper = MultiTenantVectorRetriever(base_retriever=base, graph_id="graph-A")

        result = wrapper.get_search_results(query_text="hello")

        assert [i.content for i in result.items] == ["ok-1", "ok-2"]

    def test_post_filter_drops_items_from_other_tenant_metadata(self) -> None:
        """Defense-in-depth: if a base retriever returns another tenant's item,
        the wrapper drops it before returning."""
        from oraclous_knowledge_retriever_service.multi_tenant import (
            MultiTenantVectorRetriever,
        )

        items = [
            _FakeItem(content="mine", metadata={"graph_id": "graph-A"}),
            _FakeItem(content="not-mine", metadata={"graph_id": "graph-B"}),
        ]
        base = _FakeBaseRetriever(items=items)
        wrapper = MultiTenantVectorRetriever(base_retriever=base, graph_id="graph-A")

        result = wrapper.get_search_results(query_text="hello")

        assert [i.content for i in result.items] == ["mine"]


# ---------------------------------------------------------------------------
# Attribute delegation
# ---------------------------------------------------------------------------


class TestAttributeDelegation:
    """Attributes not defined on the wrapper itself fall through to the base
    retriever — keeps the wrapper a drop-in replacement.

    Legacy reference: ``MultiTenantRetriever.__getattr__`` L86-88.
    """

    def test_unknown_attribute_falls_through_to_base(self) -> None:
        from oraclous_knowledge_retriever_service.multi_tenant import (
            MultiTenantVectorRetriever,
        )

        base = _FakeBaseRetriever()
        wrapper = MultiTenantVectorRetriever(base_retriever=base, graph_id="graph-A")
        assert wrapper.embedder is base.embedder
