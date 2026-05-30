"""Outer organisation-scoping layer above the legacy ``graph_id`` injection
(ORA-18 / Epic A3, the *Extend* step).

These tests pin the new contract: every multi-tenant retriever and the
multi-tenant writer scope by ``organisation_id`` (taken from the resolved
``OrganisationContext`` — never from a request body) before they apply the
legacy ``graph_id`` filter. ``organisation_id`` is outermost; ``graph_id`` is
inner; the existing legacy behaviour covered in
[test_multi_tenant_retrievers.py](./test_multi_tenant_retrievers.py) and
[test_multi_tenant_writer.py](./test_multi_tenant_writer.py) is preserved.

Threats mitigated: T1 (cross-tenant data leakage). The fail-closed
construction pattern aligns with ADR-006 (organisation_id on every operation)
and the [A2] enforcement story (ORA-17).

Lift-tag: Lift + extend. The "extend" surface is the new
``OrganisationScoped*`` set; the legacy ``MultiTenant*`` wrappers remain the
inner layer and keep their existing graph-scoped behaviour.

Imports of the not-yet-built seams ``oraclous_governance.context`` (ORA-14)
and ``oraclous_knowledge_retriever_service.multi_tenant`` (ORA-18 impl) are
function-local per ORA-48 / TST001 (the TDD-window collection-safety
convention): collection succeeds, each test fails RED at *runtime* with
``ModuleNotFoundError`` until the paired ``[impl]`` PRs land.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Local-import helpers (one per SUT module)
# ---------------------------------------------------------------------------


_ORG_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_SEED_ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_PRINCIPAL = uuid.UUID("99999999-9999-9999-9999-999999999999")


def _context(organisation_id: uuid.UUID = _ORG_A):
    """Build an ``OrganisationContext``; imports the seam locally."""
    from oraclous_governance.context import OrganisationContext, PrincipalType

    return OrganisationContext(
        organisation_id=organisation_id,
        principal_id=_PRINCIPAL,
        principal_type=PrincipalType.USER,
    )


def _classes():
    """Return the four SUT classes as a single tuple; imports the seam locally.

    Returned in this fixed order so each test can destructure exactly the
    subset it cares about::

        VR, VCR, HR, Writer = _classes()
    """
    from oraclous_knowledge_retriever_service.multi_tenant import (
        OrganisationScopedHybridRetriever,
        OrganisationScopedKGWriter,
        OrganisationScopedVectorCypherRetriever,
        OrganisationScopedVectorRetriever,
    )

    return (
        OrganisationScopedVectorRetriever,
        OrganisationScopedVectorCypherRetriever,
        OrganisationScopedHybridRetriever,
        OrganisationScopedKGWriter,
    )


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeItem:
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeResult:
    items: list[_FakeItem]


class _FakeBaseRetriever:
    def __init__(
        self, *, items: list[_FakeItem] | None = None, driver: object | None = None
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


@dataclass
class _Node:
    id: str
    label: str
    properties: dict[str, Any] | None = None


@dataclass
class _Rel:
    type: str
    start_node_id: str
    end_node_id: str
    properties: dict[str, Any] | None = None


@dataclass
class _Graph:
    nodes: list[_Node] = field(default_factory=list)
    relationships: list[_Rel] = field(default_factory=list)


class _CapturingBaseWriter:
    def __init__(self) -> None:
        self.runs: list[_Graph] = []
        self.driver = object()
        self.neo4j_database = "neo4j"

    async def run(self, graph: _Graph) -> None:
        self.runs.append(graph)


# ---------------------------------------------------------------------------
# Construction: organisation context is REQUIRED and fail-closed
# ---------------------------------------------------------------------------


class TestConstructionFailClosed:
    """The wrappers cannot be constructed without an authenticated
    ``OrganisationContext`` — there is no implicit / default scope.

    Threat: T1. Aligns with ADR-006 and the [A2] enforcement contract
    (ORA-17): ``organisation_id`` comes from authenticated context only,
    never from a request body or a default.
    """

    def test_vector_retriever_requires_organisation_context(self) -> None:
        VR, _VCR, _HR, _Writer = _classes()
        with pytest.raises((TypeError, ValueError)):
            VR(
                base_retriever=_FakeBaseRetriever(),
                context=None,  # type: ignore[arg-type]
                graph_id="graph-A",
            )

    def test_vector_cypher_retriever_requires_organisation_context(self) -> None:
        _VR, VCR, _HR, _Writer = _classes()
        with pytest.raises((TypeError, ValueError)):
            VCR(
                base_retriever=_FakeBaseRetriever(),
                context=None,  # type: ignore[arg-type]
                graph_id="graph-A",
            )

    def test_hybrid_retriever_requires_organisation_context(self) -> None:
        _VR, _VCR, HR, _Writer = _classes()
        with pytest.raises((TypeError, ValueError)):
            HR(
                base_retriever=_FakeBaseRetriever(),
                context=None,  # type: ignore[arg-type]
                graph_id="graph-A",
            )

    def test_writer_requires_organisation_context(self) -> None:
        _VR, _VCR, _HR, Writer = _classes()
        with pytest.raises((TypeError, ValueError)):
            Writer(
                base_writer=_CapturingBaseWriter(),
                context=None,  # type: ignore[arg-type]
                graph_id="graph-A",
            )


# ---------------------------------------------------------------------------
# Retriever: organisation_id injected outermost, graph_id inner
# ---------------------------------------------------------------------------


class TestRetrieverOrganisationScopeInjection:
    """The outer layer injects ``organisation_id`` into both ``filters`` and
    ``query_params`` before delegating; the legacy ``graph_id`` injection
    survives unchanged."""

    def test_organisation_id_added_to_filters(self) -> None:
        VR, _VCR, _HR, _Writer = _classes()
        base = _FakeBaseRetriever()
        retriever = VR(base_retriever=base, context=_context(_ORG_A), graph_id="graph-A")

        retriever.get_search_results(query_text="hi")

        filters = base.calls[0]["filters"]
        assert filters["organisation_id"] == str(_ORG_A)
        assert filters["graph_id"] == "graph-A"

    def test_organisation_id_added_to_query_params(self) -> None:
        VR, _VCR, _HR, _Writer = _classes()
        base = _FakeBaseRetriever()
        retriever = VR(base_retriever=base, context=_context(_ORG_A), graph_id="graph-A")

        retriever.get_search_results(query_text="hi")

        params = base.calls[0]["query_params"]
        assert params["organisation_id"] == str(_ORG_A)
        assert params["graph_id"] == "graph-A"

    def test_caller_supplied_organisation_id_filter_is_overwritten(self) -> None:
        """A caller passing ``filters={"organisation_id": "other"}`` MUST NOT be
        able to widen the scope. T1: prevents request-body org-id override."""
        VR, _VCR, _HR, _Writer = _classes()
        base = _FakeBaseRetriever()
        retriever = VR(base_retriever=base, context=_context(_ORG_A), graph_id="graph-A")

        retriever.get_search_results(query_text="hi", filters={"organisation_id": str(_ORG_B)})

        assert base.calls[0]["filters"]["organisation_id"] == str(_ORG_A)

    def test_caller_supplied_organisation_id_query_param_is_overwritten(self) -> None:
        VR, _VCR, _HR, _Writer = _classes()
        base = _FakeBaseRetriever()
        retriever = VR(base_retriever=base, context=_context(_ORG_A), graph_id="graph-A")

        retriever.get_search_results(query_text="hi", query_params={"organisation_id": str(_ORG_B)})

        assert base.calls[0]["query_params"]["organisation_id"] == str(_ORG_A)


# ---------------------------------------------------------------------------
# Cypher WHERE clause: organisation_id AND graph_id, parameterised
# ---------------------------------------------------------------------------


class TestCypherOrganisationScope:
    """``OrganisationScopedVectorCypherRetriever`` emits a parameterised WHERE
    clause filtering on BOTH ``organisation_id`` and ``graph_id`` so the index
    cannot return another tenant's nodes."""

    def test_where_clause_filters_both_organisation_and_graph(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _VR, VCR, _HR, _Writer = _classes()
        captured: dict[str, Any] = {}

        def _fake_base(**kwargs: Any) -> _FakeBaseRetriever:
            captured.update(kwargs)
            return _FakeBaseRetriever()

        monkeypatch.setattr(
            "oraclous_knowledge_retriever_service.multi_tenant.VectorCypherRetriever",
            _fake_base,
        )

        VCR.create(
            driver=object(),
            index_name="entity_embeddings",
            embedder=object(),
            retrieval_query="MATCH (node:Entity) RETURN node",
            context=_context(_ORG_A),
            graph_id="graph-A",
        )

        q = captured["retrieval_query"]
        assert "$organisation_id" in q
        assert "$graph_id" in q
        # parameterised, not interpolated
        assert str(_ORG_A) not in q

    def test_cypher_value_is_parameter_not_interpolated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cypher injection guard: ``organisation_id`` is never spliced into
        the query string."""
        _VR, VCR, _HR, _Writer = _classes()
        captured: dict[str, Any] = {}

        def _fake_base(**kwargs: Any) -> _FakeBaseRetriever:
            captured.update(kwargs)
            return _FakeBaseRetriever()

        monkeypatch.setattr(
            "oraclous_knowledge_retriever_service.multi_tenant.VectorCypherRetriever",
            _fake_base,
        )

        VCR.create(
            driver=object(),
            index_name="entity_embeddings",
            embedder=object(),
            retrieval_query="MATCH (node:Entity) RETURN node",
            context=_context(_ORG_A),
            graph_id="graph-A",
        )

        assert str(_ORG_A) not in captured["retrieval_query"]


# ---------------------------------------------------------------------------
# Tenant index naming includes organisation_id
# ---------------------------------------------------------------------------


class TestTenantIndexNamingIncludesOrganisation:
    """The tenant-specific index name must include the organisation id so two
    organisations sharing the same ``graph_id`` (or a malicious caller guessing
    a base index name) cannot read each other's vectors."""

    def test_vector_index_name_carries_organisation_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        VR, _VCR, _HR, _Writer = _classes()
        captured: dict[str, Any] = {}

        def _fake_base(**kwargs: Any) -> _FakeBaseRetriever:
            captured.update(kwargs)
            return _FakeBaseRetriever()

        monkeypatch.setattr(
            "oraclous_knowledge_retriever_service.multi_tenant.VectorRetriever",
            _fake_base,
        )

        VR.create(
            driver=object(),
            index_name="entity_embeddings",
            embedder=object(),
            context=_context(_ORG_A),
            graph_id="graph-A",
        )

        assert str(_ORG_A) in captured["index_name"]
        assert "graph-A" in captured["index_name"]

    def test_hybrid_indices_carry_organisation_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _VR, _VCR, HR, _Writer = _classes()
        captured: dict[str, Any] = {}

        def _fake_base(**kwargs: Any) -> _FakeBaseRetriever:
            captured.update(kwargs)
            return _FakeBaseRetriever()

        monkeypatch.setattr(
            "oraclous_knowledge_retriever_service.multi_tenant.HybridRetriever",
            _fake_base,
        )

        HR.create(
            driver=object(),
            vector_index_name="entity_embeddings",
            fulltext_index_name="entity_text_fulltext",
            embedder=object(),
            context=_context(_ORG_A),
            graph_id="graph-A",
        )

        assert str(_ORG_A) in captured["vector_index_name"]
        assert str(_ORG_A) in captured["fulltext_index_name"]


# ---------------------------------------------------------------------------
# Post-filter back-stop on organisation_id
# ---------------------------------------------------------------------------


class TestOrganisationPostFilterBackstop:
    """Defence-in-depth: if the base retriever returns an item that lacks the
    expected ``organisation_id`` (or claims another organisation), the wrapper
    drops it before returning."""

    def test_drops_items_from_other_organisation_even_when_graph_id_matches(
        self,
    ) -> None:
        VR, _VCR, _HR, _Writer = _classes()
        items = [
            _FakeItem(
                content="ours",
                metadata={"organisation_id": str(_ORG_A), "graph_id": "graph-A"},
            ),
            # Same graph_id, but a different organisation — must NOT be returned.
            _FakeItem(
                content="theirs",
                metadata={"organisation_id": str(_ORG_B), "graph_id": "graph-A"},
            ),
        ]
        base = _FakeBaseRetriever(items=items)
        retriever = VR(base_retriever=base, context=_context(_ORG_A), graph_id="graph-A")

        result = retriever.get_search_results(query_text="hi")

        assert [i.content for i in result.items] == ["ours"]

    def test_drops_items_with_missing_organisation_id_metadata(self) -> None:
        """Fail-closed: an item with no ``organisation_id`` metadata is
        indeterminate and is dropped, never returned."""
        VR, _VCR, _HR, _Writer = _classes()
        items = [
            _FakeItem(
                content="ours",
                metadata={"organisation_id": str(_ORG_A), "graph_id": "graph-A"},
            ),
            _FakeItem(content="orphan", metadata={"graph_id": "graph-A"}),
        ]
        base = _FakeBaseRetriever(items=items)
        retriever = VR(base_retriever=base, context=_context(_ORG_A), graph_id="graph-A")

        result = retriever.get_search_results(query_text="hi")

        assert [i.content for i in result.items] == ["ours"]


# ---------------------------------------------------------------------------
# Writer: organisation_id injected, unconditionally overwritten
# ---------------------------------------------------------------------------


class TestWriterOrganisationScopeInjection:
    """The outer writer layer stamps ``organisation_id`` on every node and
    relationship, unconditionally overriding any caller-supplied value (T1).

    Mirrors the existing ``graph_id`` security contract proven in
    [test_multi_tenant_writer.py::TestGraphIdOverwriteSecurity]."""

    async def test_writer_injects_organisation_id_on_every_node(self) -> None:
        _VR, _VCR, _HR, Writer = _classes()
        base = _CapturingBaseWriter()
        writer = Writer(base_writer=base, context=_context(_ORG_A), graph_id="graph-A")
        graph = _Graph(
            nodes=[
                _Node(id="n1", label="Person", properties={"name": "Alice"}),
                _Node(id="n2", label="Person", properties={"name": "Bob"}),
            ]
        )

        await writer.run(graph)

        assert all(n.properties["organisation_id"] == str(_ORG_A) for n in graph.nodes)

    async def test_writer_injects_organisation_id_on_every_relationship(self) -> None:
        _VR, _VCR, _HR, Writer = _classes()
        base = _CapturingBaseWriter()
        writer = Writer(base_writer=base, context=_context(_ORG_A), graph_id="graph-A")
        graph = _Graph(
            nodes=[
                _Node(id="a", label="Person", properties={"name": "Alice"}),
                _Node(id="b", label="Person", properties={"name": "Bob"}),
            ],
            relationships=[
                _Rel(type="KNOWS", start_node_id="a", end_node_id="b"),
            ],
        )

        await writer.run(graph)

        assert graph.relationships[0].properties["organisation_id"] == str(_ORG_A)

    async def test_writer_unconditionally_overwrites_caller_supplied_organisation_id_on_node(
        self,
    ) -> None:
        """T1: a node whose properties include a different ``organisation_id``
        MUST NOT be written to that other organisation."""
        _VR, _VCR, _HR, Writer = _classes()
        base = _CapturingBaseWriter()
        writer = Writer(base_writer=base, context=_context(_ORG_A), graph_id="graph-A")
        graph = _Graph(
            nodes=[
                _Node(
                    id="n1",
                    label="Person",
                    properties={"name": "Alice", "organisation_id": str(_ORG_B)},
                )
            ]
        )

        await writer.run(graph)

        assert graph.nodes[0].properties["organisation_id"] == str(_ORG_A)

    async def test_writer_unconditionally_overwrites_caller_organisation_id_on_rel(
        self,
    ) -> None:
        _VR, _VCR, _HR, Writer = _classes()
        base = _CapturingBaseWriter()
        writer = Writer(base_writer=base, context=_context(_ORG_A), graph_id="graph-A")
        graph = _Graph(
            nodes=[
                _Node(id="a", label="Person", properties={"name": "Alice"}),
                _Node(id="b", label="Person", properties={"name": "Bob"}),
            ],
            relationships=[
                _Rel(
                    type="KNOWS",
                    start_node_id="a",
                    end_node_id="b",
                    properties={"organisation_id": str(_ORG_B)},
                )
            ],
        )

        await writer.run(graph)

        assert graph.relationships[0].properties["organisation_id"] == str(_ORG_A)

    async def test_writer_preserves_legacy_graph_id_alongside_organisation_id(
        self,
    ) -> None:
        """``organisation_id`` is added *alongside* ``graph_id`` — the inner
        ``graph_id`` boundary survives unchanged (Lift step preserved)."""
        _VR, _VCR, _HR, Writer = _classes()
        base = _CapturingBaseWriter()
        writer = Writer(base_writer=base, context=_context(_ORG_A), graph_id="graph-A")
        graph = _Graph(nodes=[_Node(id="n1", label="Person", properties={"name": "Alice"})])

        await writer.run(graph)

        props = graph.nodes[0].properties
        assert props["organisation_id"] == str(_ORG_A)
        assert props["graph_id"] == "graph-A"


# ---------------------------------------------------------------------------
# Single-org regression: seed-org context preserves legacy behaviour
# ---------------------------------------------------------------------------


class TestSingleOrgRegression:
    """Acceptance criterion: "Single-organisation behaviour unchanged".

    When the resolved context is the seed organisation, the wrappers behave
    identically to the legacy single-graph_id wrapper — ``organisation_id``
    is injected but the graph_id-only behaviour matches what existed before.
    """

    def test_retriever_in_seed_org_returns_same_items_as_graph_id_filter_alone(
        self,
    ) -> None:
        """With a single org and matching graph_id, all items pass through."""
        VR, _VCR, _HR, _Writer = _classes()
        items = [
            _FakeItem(
                content="one",
                metadata={"organisation_id": str(_SEED_ORG), "graph_id": "graph-A"},
            ),
            _FakeItem(
                content="two",
                metadata={"organisation_id": str(_SEED_ORG), "graph_id": "graph-A"},
            ),
        ]
        base = _FakeBaseRetriever(items=items)
        retriever = VR(base_retriever=base, context=_context(_SEED_ORG), graph_id="graph-A")

        result = retriever.get_search_results(query_text="hi")

        assert [i.content for i in result.items] == ["one", "two"]

    async def test_writer_in_seed_org_writes_nodes_unchanged_except_for_org_id(
        self,
    ) -> None:
        """Single-org regression: the only new property is ``organisation_id``;
        all legacy properties (``graph_id``, timestamps, ``created_by``) are
        identical to the lift-only flow."""
        _VR, _VCR, _HR, Writer = _classes()
        base = _CapturingBaseWriter()
        writer = Writer(base_writer=base, context=_context(_SEED_ORG), graph_id="graph-A")
        graph = _Graph(nodes=[_Node(id="n1", label="Person", properties={"name": "Alice"})])

        await writer.run(graph)

        props = graph.nodes[0].properties
        assert props["organisation_id"] == str(_SEED_ORG)
        assert props["graph_id"] == "graph-A"
        assert "transaction_time" in props
        assert "ingestion_time" in props
        assert props["created_by"] == "multi_tenant_pipeline"
