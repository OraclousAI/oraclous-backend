"""Outer organisation-scoping layer above the legacy ``graph_id`` injection
for the three multi-tenant retrievers (ORA-18 / Epic A3, the *Extend* step —
retriever half).

These tests pin the new contract for the ``OrganisationScoped*`` retriever
surface: scope by ``organisation_id`` (taken from the resolved
``OrganisationContext`` — never from a request body) before applying the
legacy ``graph_id`` filter. ``organisation_id`` is outermost; ``graph_id`` is
inner; the existing legacy retriever behaviour covered in
[test_multi_tenant_retrievers.py](./test_multi_tenant_retrievers.py) is
preserved.

Threats mitigated: T1 (cross-tenant data leakage). The fail-closed
construction pattern aligns with ADR-006 (organisation_id on every operation)
and the [A2] enforcement story (ORA-17).

Module placement: ``solution-architect`` ratified Option B (split) on
31 May 2026 — the three retrievers live in ``knowledge-retriever-service``
(read path); the writer's organisation-scoping tests live in
``services/knowledge-graph-service/tests/unit/test_organisation_scoped_writer.py``.
Both consume the substrate seam ``oraclous_substrate.access`` per
[ADR-012](https://oraclous.atlassian.net/wiki/spaces/OP/pages/2490396) §1;
neither forks org-scoping.

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
from oraclous_governance.propagation import (
    MissingOrganisationContextError,
    use_organisation_context,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Local-import helpers
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


def _retrievers():
    """Return the three retriever SUT classes; imports the seam locally.

    Returned in this fixed order so each test can destructure exactly the
    subset it cares about::

        VR, VCR, HR = _retrievers()
    """
    from oraclous_knowledge_retriever_service.multi_tenant import (
        OrganisationScopedHybridRetriever,
        OrganisationScopedVectorCypherRetriever,
        OrganisationScopedVectorRetriever,
    )

    return (
        OrganisationScopedVectorRetriever,
        OrganisationScopedVectorCypherRetriever,
        OrganisationScopedHybridRetriever,
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


# ---------------------------------------------------------------------------
# Construction: organisation context is REQUIRED and fail-closed
# ---------------------------------------------------------------------------


class TestRuntimeFailClosed:
    """The retriever wrappers fail closed at *runtime* when no
    ``OrganisationContext`` is bound — there is no implicit / default scope.

    The 'no implicit / default scope' invariant originally lived at
    construction (ORA-18) but moved to runtime in ORA-52 / ADR-012 §1b: the
    wrapper sources its ``organisation_id`` live from the substrate seam
    (``current_organisation_context()``) so a request body cannot redirect
    scope at construction. The same fail-closed guarantee is preserved —
    just at the runtime boundary instead of ``__init__``.

    Threat: T1-M1.
    """

    pytestmark = [pytest.mark.unit, pytest.mark.security]

    def test_vector_retriever_fails_closed_without_bound_context_at_runtime(
        self,
    ) -> None:
        VR, _VCR, _HR = _retrievers()
        retriever = VR(base_retriever=_FakeBaseRetriever(), graph_id="graph-A")
        with pytest.raises((MissingOrganisationContextError, ValueError, RuntimeError)):
            retriever.get_search_results(query_text="hi")

    def test_vector_cypher_retriever_fails_closed_without_bound_context_at_runtime(
        self,
    ) -> None:
        _VR, VCR, _HR = _retrievers()
        retriever = VCR(base_retriever=_FakeBaseRetriever(), graph_id="graph-A")
        with pytest.raises((MissingOrganisationContextError, ValueError, RuntimeError)):
            retriever.get_search_results(query_text="hi")

    def test_hybrid_retriever_fails_closed_without_bound_context_at_runtime(
        self,
    ) -> None:
        _VR, _VCR, HR = _retrievers()
        retriever = HR(base_retriever=_FakeBaseRetriever(), graph_id="graph-A")
        with pytest.raises((MissingOrganisationContextError, ValueError, RuntimeError)):
            retriever.get_search_results(query_text="hi")


# ---------------------------------------------------------------------------
# Retriever: organisation_id injected outermost, graph_id inner
# ---------------------------------------------------------------------------


class TestRetrieverOrganisationScopeInjection:
    """The outer layer injects ``organisation_id`` into both ``filters`` and
    ``query_params`` before delegating; the legacy ``graph_id`` injection
    survives unchanged."""

    def test_organisation_id_added_to_filters(self) -> None:
        VR, _VCR, _HR = _retrievers()
        base = _FakeBaseRetriever()
        retriever = VR(base_retriever=base, graph_id="graph-A")

        with use_organisation_context(_context(_ORG_A)):
            retriever.get_search_results(query_text="hi")

        filters = base.calls[0]["filters"]
        assert filters["organisation_id"] == str(_ORG_A)
        assert filters["graph_id"] == "graph-A"

    def test_organisation_id_added_to_query_params(self) -> None:
        VR, _VCR, _HR = _retrievers()
        base = _FakeBaseRetriever()
        retriever = VR(base_retriever=base, graph_id="graph-A")

        with use_organisation_context(_context(_ORG_A)):
            retriever.get_search_results(query_text="hi")

        params = base.calls[0]["query_params"]
        assert params["organisation_id"] == str(_ORG_A)
        assert params["graph_id"] == "graph-A"

    @pytest.mark.security
    def test_caller_supplied_organisation_id_filter_is_overwritten(self) -> None:
        """A caller passing ``filters={"organisation_id": "other"}`` MUST NOT be
        able to widen the scope. T1: prevents request-body org-id override."""
        VR, _VCR, _HR = _retrievers()
        base = _FakeBaseRetriever()
        retriever = VR(base_retriever=base, graph_id="graph-A")

        with use_organisation_context(_context(_ORG_A)):
            retriever.get_search_results(query_text="hi", filters={"organisation_id": str(_ORG_B)})

        assert base.calls[0]["filters"]["organisation_id"] == str(_ORG_A)

    @pytest.mark.security
    def test_caller_supplied_organisation_id_query_param_is_overwritten(self) -> None:
        VR, _VCR, _HR = _retrievers()
        base = _FakeBaseRetriever()
        retriever = VR(base_retriever=base, graph_id="graph-A")

        with use_organisation_context(_context(_ORG_A)):
            retriever.get_search_results(
                query_text="hi", query_params={"organisation_id": str(_ORG_B)}
            )

        assert base.calls[0]["query_params"]["organisation_id"] == str(_ORG_A)


# ---------------------------------------------------------------------------
# Cypher WHERE clause: organisation_id AND graph_id, parameterised
# ---------------------------------------------------------------------------


class TestCypherOrganisationScope:
    """``OrganisationScopedVectorCypherRetriever`` emits a parameterised WHERE
    clause filtering on BOTH ``organisation_id`` and ``graph_id`` so the index
    cannot return another tenant's nodes."""

    pytestmark = [pytest.mark.unit, pytest.mark.security]

    def test_where_clause_filters_both_organisation_and_graph(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _VR, VCR, _HR = _retrievers()
        captured: dict[str, Any] = {}

        def _fake_base(**kwargs: Any) -> _FakeBaseRetriever:
            captured.update(kwargs)
            return _FakeBaseRetriever()

        monkeypatch.setattr(
            "oraclous_knowledge_retriever_service.multi_tenant.VectorCypherRetriever",
            _fake_base,
        )

        with use_organisation_context(_context(_ORG_A)):
            VCR.create(
                driver=object(),
                index_name="entity_embeddings",
                embedder=object(),
                retrieval_query="MATCH (node:Entity) RETURN node",
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
        _VR, VCR, _HR = _retrievers()
        captured: dict[str, Any] = {}

        def _fake_base(**kwargs: Any) -> _FakeBaseRetriever:
            captured.update(kwargs)
            return _FakeBaseRetriever()

        monkeypatch.setattr(
            "oraclous_knowledge_retriever_service.multi_tenant.VectorCypherRetriever",
            _fake_base,
        )

        with use_organisation_context(_context(_ORG_A)):
            VCR.create(
                driver=object(),
                index_name="entity_embeddings",
                embedder=object(),
                retrieval_query="MATCH (node:Entity) RETURN node",
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
        VR, _VCR, _HR = _retrievers()
        captured: dict[str, Any] = {}

        def _fake_base(**kwargs: Any) -> _FakeBaseRetriever:
            captured.update(kwargs)
            return _FakeBaseRetriever()

        monkeypatch.setattr(
            "oraclous_knowledge_retriever_service.multi_tenant.VectorRetriever",
            _fake_base,
        )

        with use_organisation_context(_context(_ORG_A)):
            VR.create(
                driver=object(),
                index_name="entity_embeddings",
                embedder=object(),
                graph_id="graph-A",
            )

        assert str(_ORG_A) in captured["index_name"]
        assert "graph-A" in captured["index_name"]

    def test_hybrid_indices_carry_organisation_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _VR, _VCR, HR = _retrievers()
        captured: dict[str, Any] = {}

        def _fake_base(**kwargs: Any) -> _FakeBaseRetriever:
            captured.update(kwargs)
            return _FakeBaseRetriever()

        monkeypatch.setattr(
            "oraclous_knowledge_retriever_service.multi_tenant.HybridRetriever",
            _fake_base,
        )

        with use_organisation_context(_context(_ORG_A)):
            HR.create(
                driver=object(),
                vector_index_name="entity_embeddings",
                fulltext_index_name="entity_text_fulltext",
                embedder=object(),
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

    pytestmark = [pytest.mark.unit, pytest.mark.security]

    def test_drops_items_from_other_organisation_even_when_graph_id_matches(
        self,
    ) -> None:
        VR, _VCR, _HR = _retrievers()
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
        retriever = VR(base_retriever=base, graph_id="graph-A")

        with use_organisation_context(_context(_ORG_A)):
            result = retriever.get_search_results(query_text="hi")

        assert [i.content for i in result.items] == ["ours"]

    def test_drops_items_with_missing_organisation_id_metadata(self) -> None:
        """Fail-closed: an item with no ``organisation_id`` metadata is
        indeterminate and is dropped, never returned."""
        VR, _VCR, _HR = _retrievers()
        items = [
            _FakeItem(
                content="ours",
                metadata={"organisation_id": str(_ORG_A), "graph_id": "graph-A"},
            ),
            _FakeItem(content="orphan", metadata={"graph_id": "graph-A"}),
        ]
        base = _FakeBaseRetriever(items=items)
        retriever = VR(base_retriever=base, graph_id="graph-A")

        with use_organisation_context(_context(_ORG_A)):
            result = retriever.get_search_results(query_text="hi")

        assert [i.content for i in result.items] == ["ours"]


# ---------------------------------------------------------------------------
# Single-org regression: seed-org context preserves legacy behaviour
# ---------------------------------------------------------------------------


class TestSingleOrgRegression:
    """Acceptance criterion: "Single-organisation behaviour unchanged".

    With the seed organisation, the retriever behaves identically to the
    legacy single-graph_id wrapper — ``organisation_id`` is injected but the
    graph_id-only behaviour matches what existed before.
    """

    def test_retriever_in_seed_org_returns_same_items_as_graph_id_filter_alone(
        self,
    ) -> None:
        """With a single org and matching graph_id, all items pass through."""
        VR, _VCR, _HR = _retrievers()
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
        retriever = VR(base_retriever=base, graph_id="graph-A")

        with use_organisation_context(_context(_SEED_ORG)):
            result = retriever.get_search_results(query_text="hi")

        assert [i.content for i in result.items] == ["one", "two"]
