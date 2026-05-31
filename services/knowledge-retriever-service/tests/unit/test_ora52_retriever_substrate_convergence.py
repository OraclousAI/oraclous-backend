"""ORA-52 — converge multi-tenant retriever wrappers onto the substrate
org-scoping helper + authenticated-context binding, and tighten the
post-filter back-stop (follow-up to ORA-18 Code Review).

This module pins the three follow-up acceptance criteria on the retriever
side:

1. **AC1 — Single source of truth (ADR-012 §1/§1b).**
   ``_build_scoped_query`` and ``OrganisationScopedRetriever._inject_runtime_scope``
   compose the canonical substrate helpers
   (``oraclous_substrate.access.ORGANISATION_ID_PROPERTY`` and
   ``oraclous_substrate.access.org_scope_predicate``) rather than re-deriving
   the property name or the predicate spelling inline.

2. **AC2 — Authenticated-context binding (T1-M1; security-architect required).**
   Every ``OrganisationScoped*Retriever`` sources its scope from the bound
   governance context (``current_organisation_context()``), never from a
   constructor argument or request body. A spoofed construction-time
   argument cannot redirect runtime scope.

3. **AC3 — Strict ``graph_id`` equality in the post-filter back-stop
   (security-architect hygiene).** ``MultiTenantRetriever._keep_item`` no
   longer falls back to a ``graph_id``-substring-in-``content`` heuristic;
   strict metadata equality is required (mirrors the ``organisation_id``
   contract already proven on the org-scoped subclass).

Imports of substrate symbols that may not yet exist (``ORGANISATION_ID_PROPERTY``,
``org_scope_predicate``) are function-local per ORA-48 / TST001 so collection
succeeds; tests fail at runtime with ``ImportError`` until the paired
``[impl]`` PR lands.

Threats: T1 (cross-tenant retrieval, request-body org-id spoofing).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from oraclous_governance.context import OrganisationContext, PrincipalType
from oraclous_governance.propagation import (
    MissingOrganisationContextError,
    use_organisation_context,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Identifiers + test doubles
# ---------------------------------------------------------------------------


_ORG_AUTHENTIC = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG_SPOOFED = uuid.UUID("22222222-2222-2222-2222-222222222222")
_PRINCIPAL = uuid.UUID("99999999-9999-9999-9999-999999999999")


def _context(org: uuid.UUID = _ORG_AUTHENTIC) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=org,
        principal_id=_PRINCIPAL,
        principal_type=PrincipalType.USER,
    )


@dataclass
class _FakeItem:
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeContentOnlyItem:
    """An item with no ``metadata`` attribute, only ``content``.

    Used by AC3 to prove the substring-in-content back-stop fallback is
    GONE — under the new contract such an item is indeterminate and dropped.
    """

    content: str


@dataclass
class _FakeResult:
    items: list[Any]


class _FakeBaseRetriever:
    def __init__(self, *, items: list[Any] | None = None) -> None:
        self.driver = object()
        self.calls: list[dict[str, Any]] = []
        self._items = items or []

    def get_search_results(
        self,
        query_vector: list[float] | None = None,
        query_text: str | None = None,
        **kwargs: Any,
    ) -> _FakeResult:
        self.calls.append({"query_vector": query_vector, "query_text": query_text, **kwargs})
        return _FakeResult(items=list(self._items))


def _retriever_classes():
    """Return the org-scoped retriever classes (local import per TST001)."""
    from oraclous_knowledge_retriever_service.multi_tenant import (
        MultiTenantRetriever,
        OrganisationScopedHybridRetriever,
        OrganisationScopedRetriever,
        OrganisationScopedVectorCypherRetriever,
        OrganisationScopedVectorRetriever,
    )

    return {
        "Base": MultiTenantRetriever,
        "Org": OrganisationScopedRetriever,
        "VR": OrganisationScopedVectorRetriever,
        "VCR": OrganisationScopedVectorCypherRetriever,
        "HR": OrganisationScopedHybridRetriever,
    }


# ---------------------------------------------------------------------------
# AC1 — Composition of the substrate org-scope helper (no inline re-derive)
# ---------------------------------------------------------------------------


class TestRetrieverComposesSubstrateHelper:
    """``_build_scoped_query`` and ``_inject_runtime_scope`` MUST compose the
    canonical substrate helpers, not re-derive the property name or predicate.
    Verified by patching the substrate helper symbols and observing the
    sentinel in the retriever's downstream output.
    """

    pytestmark = [pytest.mark.unit, pytest.mark.security]

    def test_build_scoped_query_composes_org_scope_predicate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The retriever's Cypher composer must source the org predicate
        fragment from the substrate helper. If it re-derives the fragment
        locally the sentinel never appears (AC1 drift)."""
        from oraclous_substrate import access

        sentinel = "SENTINEL_ORG_PRED.organisation_id = $organisation_id"
        monkeypatch.setattr(
            access,
            "org_scope_predicate",
            lambda alias="node": sentinel,
        )

        from oraclous_knowledge_retriever_service.multi_tenant import _build_scoped_query

        scoped = _build_scoped_query("MATCH (node:Entity) RETURN node")

        assert sentinel in scoped, (
            "_build_scoped_query must compose oraclous_substrate.access."
            "org_scope_predicate (AC1). Inline re-derivation is forbidden."
        )

    def test_inject_runtime_scope_uses_canonical_property_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Runtime ``filters`` and ``query_params`` key the organisation id
        under the canonical constant — not under an inlined literal. Patch
        the constant to a sentinel name and observe the sentinel as the key.
        """
        from oraclous_substrate import access

        sentinel_property = "SENTINEL_organisation_id"
        monkeypatch.setattr(access, "ORGANISATION_ID_PROPERTY", sentinel_property)

        klasses = _retriever_classes()
        base = _FakeBaseRetriever()
        wrapper = klasses["Org"](
            base_retriever=base,
            context=_context(_ORG_AUTHENTIC),
            graph_id="graph-A",
        )

        with use_organisation_context(_context(_ORG_AUTHENTIC)):
            wrapper.get_search_results(query_text="hi")

        call = base.calls[0]
        assert sentinel_property in call["filters"], (
            "_inject_runtime_scope must key organisation_id via ORGANISATION_ID_PROPERTY (AC1)."
        )
        assert sentinel_property in call["query_params"], (
            "_inject_runtime_scope must key organisation_id via "
            "ORGANISATION_ID_PROPERTY for query_params as well (AC1)."
        )


# ---------------------------------------------------------------------------
# AC2 — Authenticated-context binding (request body cannot redirect scope)
# ---------------------------------------------------------------------------


class TestRetrieverAuthenticatedContextBinding:
    """The retriever's effective organisation scope MUST come from the bound
    ``current_organisation_context()`` — never from a constructor argument or
    a request body.

    Two complementary properties pin this:

    * Construction without a bound context fails closed (no implicit / default
      scope at construction time).
    * A construction-time argument purporting to set a different organisation
      cannot redirect scope: either the constructor refuses the kwarg
      (``TypeError``), or the wrapper ignores its value and tracks the bound
      context at runtime.
    """

    pytestmark = [pytest.mark.unit, pytest.mark.security]

    def test_vector_create_fails_closed_when_no_context_bound(self) -> None:
        """No bound context → construction halts (T1-M1 fail-closed)."""
        klasses = _retriever_classes()

        from neo4j_graphrag.embeddings.base import Embedder

        class _FakeEmbedder(Embedder):
            def embed_query(self, text: str) -> list[float]:
                return [0.0]

        with pytest.raises((MissingOrganisationContextError, ValueError, TypeError)):
            # The retriever must source its scope from the seam; no bound
            # context = no scope = refusal. The exact exception type is up to
            # the implementer (helper raise vs. own raise).
            klasses["VR"].create(
                driver=object(),
                index_name="idx",
                embedder=_FakeEmbedder(),
                graph_id="graph-A",
            )

    def test_runtime_injection_tracks_bound_context_not_construction_arg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_inject_runtime_scope`` resolves ``organisation_id`` from the
        bound context at *runtime*. Binding context X then calling
        ``get_search_results`` must produce filters scoped to X — even if the
        wrapper was previously constructed under a different context.
        """
        klasses = _retriever_classes()
        base = _FakeBaseRetriever()

        # Construct under the authentic context.
        with use_organisation_context(_context(_ORG_AUTHENTIC)):
            wrapper = klasses["Org"](
                base_retriever=base,
                context=_context(_ORG_AUTHENTIC),
                graph_id="graph-A",
            )

        # Now run under the same authentic context — the bound value wins.
        with use_organisation_context(_context(_ORG_AUTHENTIC)):
            wrapper.get_search_results(query_text="hi")

        filters = base.calls[-1]["filters"]
        assert filters["organisation_id"] == str(_ORG_AUTHENTIC), (
            "Runtime scope must equal the BOUND context's organisation_id "
            "(AC2). If this fails the wrapper is reading a stale "
            "construction-time arg instead of the seam."
        )

    def test_runtime_injection_fails_closed_when_no_context_bound_at_call(
        self,
    ) -> None:
        """If the bound context disappears between construction and runtime
        (e.g. a request handler dropped it), the wrapper must fail closed at
        the call site rather than silently fall back to a constructor arg
        (T1-M1)."""
        klasses = _retriever_classes()
        base = _FakeBaseRetriever()

        with use_organisation_context(_context(_ORG_AUTHENTIC)):
            wrapper = klasses["Org"](
                base_retriever=base,
                context=_context(_ORG_AUTHENTIC),
                graph_id="graph-A",
            )

        # No bound context here — the wrapper must refuse to run.
        with pytest.raises((MissingOrganisationContextError, ValueError, RuntimeError)):
            wrapper.get_search_results(query_text="hi")

    def test_construction_arg_cannot_redirect_runtime_scope(self) -> None:
        """Spoofing attempt: bind authentic org A, supply spoofed org B as a
        construction-time context argument. The wrapper's *runtime* scope must
        still be A — never B. Acceptable implementations:

        * Drop the ``context=`` kwarg entirely (``TypeError`` at construction).
        * Keep the kwarg but ignore it; runtime reads the bound context.
        * Keep the kwarg but raise on mismatch with the bound context.

        Any of the three satisfies the invariant ``runtime scope == bound``.
        """
        klasses = _retriever_classes()
        base = _FakeBaseRetriever()

        with use_organisation_context(_context(_ORG_AUTHENTIC)):
            try:
                wrapper = klasses["Org"](
                    base_retriever=base,
                    context=_context(_ORG_SPOOFED),  # the spoof attempt
                    graph_id="graph-A",
                )
            except (TypeError, ValueError):
                # Construction refused the spoof (kwarg dropped, or
                # mismatch-on-bound enforced). Invariant satisfied — STOP.
                return

            wrapper.get_search_results(query_text="hi")

        filters = base.calls[-1]["filters"]
        assert filters["organisation_id"] == str(_ORG_AUTHENTIC), (
            "AC2: a constructor argument MUST NOT redirect scope. The runtime "
            f"scope was {filters['organisation_id']!r} but the bound context "
            f"was {_ORG_AUTHENTIC!s}."
        )
        assert filters["organisation_id"] != str(_ORG_SPOOFED), (
            "AC2: the spoofed construction-time organisation_id leaked into "
            "runtime scope. Request-body redirection is a T1-M1 violation."
        )

    def test_post_filter_uses_bound_context_not_construction_arg(self) -> None:
        """The post-filter back-stop checks items' metadata against the
        bound context's organisation_id — never the construction-time arg's.
        An item bearing the SPOOFED org_id must be dropped under the
        AUTHENTIC bound context."""
        klasses = _retriever_classes()
        items = [
            _FakeItem(
                content="leaked",
                metadata={
                    "organisation_id": str(_ORG_SPOOFED),
                    "graph_id": "graph-A",
                },
            ),
            _FakeItem(
                content="ours",
                metadata={
                    "organisation_id": str(_ORG_AUTHENTIC),
                    "graph_id": "graph-A",
                },
            ),
        ]
        base = _FakeBaseRetriever(items=items)

        with use_organisation_context(_context(_ORG_AUTHENTIC)):
            try:
                wrapper = klasses["Org"](
                    base_retriever=base,
                    context=_context(_ORG_SPOOFED),
                    graph_id="graph-A",
                )
            except (TypeError, ValueError):
                # Construction refused the spoof — invariant satisfied.
                return
            result = wrapper.get_search_results(query_text="hi")

        kept = [i.content for i in result.items]
        assert "leaked" not in kept, (
            "AC2: the post-filter dropped the bound-context match instead of "
            "the spoofed one — scope was redirected by the construction arg."
        )
        assert kept == ["ours"], (
            "AC2: only items matching the BOUND context's organisation_id "
            f"survive the post-filter; got {kept!r}."
        )


# ---------------------------------------------------------------------------
# AC3 — Strict graph_id equality in the post-filter back-stop
# ---------------------------------------------------------------------------


class TestKeepItemStrictGraphIdEquality:
    """``MultiTenantRetriever._keep_item`` no longer falls back to a
    ``graph_id``-substring-in-``content`` heuristic. Items must match by
    strict metadata equality on ``graph_id`` (mirroring the ``organisation_id``
    contract already proven on the org-scoped subclass).

    Security rationale: substring-on-content trusts LLM-generated text to
    decide tenancy. A document body that happens to mention another tenant's
    graph id should NOT be misclassified as belonging to that tenant. Strict
    metadata equality is the only safe criterion (security-architect hygiene
    finding on ORA-18 review).
    """

    pytestmark = [pytest.mark.unit, pytest.mark.security]

    def test_keep_drops_item_when_metadata_graph_id_missing(self) -> None:
        """An item with no ``metadata`` attribute is indeterminate and
        dropped — the current substring-fallback would have kept it on the
        strength of content alone. AC3 closes that gap."""
        klasses = _retriever_classes()
        base = _FakeBaseRetriever()
        wrapper = klasses["Base"](base_retriever=base, graph_id="graph-A")

        item = _FakeContentOnlyItem(content="this body mentions graph-A inside")

        assert wrapper._keep_item(item) is False, (
            "AC3: a content-only item must be dropped; the substring fallback must be gone."
        )

    def test_keep_drops_item_when_metadata_lacks_graph_id_key(self) -> None:
        """An item with ``metadata`` but no ``graph_id`` key is dropped —
        fail-closed on indeterminate (matches the ``organisation_id``
        contract)."""
        klasses = _retriever_classes()
        base = _FakeBaseRetriever()
        wrapper = klasses["Base"](base_retriever=base, graph_id="graph-A")

        item = _FakeItem(
            content="graph-A appears in body",
            metadata={"organisation_id": str(_ORG_AUTHENTIC)},
        )

        assert wrapper._keep_item(item) is False, (
            "AC3: metadata.graph_id is required; absent metadata.graph_id is indeterminate → drop."
        )

    def test_keep_drops_item_when_metadata_graph_id_mismatch(self) -> None:
        klasses = _retriever_classes()
        base = _FakeBaseRetriever()
        wrapper = klasses["Base"](base_retriever=base, graph_id="graph-A")

        item = _FakeItem(
            content="anything",
            metadata={"graph_id": "graph-B"},
        )

        assert wrapper._keep_item(item) is False

    def test_keep_drops_item_with_content_substring_but_no_metadata_match(
        self,
    ) -> None:
        """The deletion-target case: an item whose ``content`` contains the
        tenant's ``graph_id`` as a substring, but whose ``metadata.graph_id``
        does NOT equal it. Currently kept by the substring fallback; under
        AC3 it MUST be dropped.

        Security: an attacker who controls document content could otherwise
        cause cross-graph reads by embedding another tenant's graph id in
        the text.
        """
        klasses = _retriever_classes()
        base = _FakeBaseRetriever()
        wrapper = klasses["Base"](base_retriever=base, graph_id="graph-A")

        item = _FakeItem(
            content="leaked body containing graph-A inline",
            metadata={"graph_id": "graph-B"},
        )

        assert wrapper._keep_item(item) is False, (
            "AC3: an item whose content substring-matches our graph_id but "
            "whose metadata.graph_id is a different tenant MUST be dropped."
        )

    def test_keep_keeps_item_when_metadata_graph_id_equal(self) -> None:
        """The happy path: strict metadata equality keeps the item."""
        klasses = _retriever_classes()
        base = _FakeBaseRetriever()
        wrapper = klasses["Base"](base_retriever=base, graph_id="graph-A")

        item = _FakeItem(
            content="anything",
            metadata={"graph_id": "graph-A"},
        )

        assert wrapper._keep_item(item) is True

    def test_org_scoped_post_filter_drops_content_substring_attack(self) -> None:
        """End-to-end on the org-scoped subclass: an item whose content
        substring-matches the tenant's graph_id but whose metadata.graph_id
        is wrong is dropped by the back-stop (AC3 composed with the
        organisation_id back-stop already proven on
        ``test_organisation_scoping_layer``).
        """
        klasses = _retriever_classes()
        items = [
            _FakeItem(
                content="kept",
                metadata={
                    "organisation_id": str(_ORG_AUTHENTIC),
                    "graph_id": "graph-A",
                },
            ),
            # Substring attack: content names graph-A but metadata claims another
            # graph. Currently leaks under the substring fallback; AC3 drops it.
            _FakeItem(
                content="prefix graph-A suffix",
                metadata={
                    "organisation_id": str(_ORG_AUTHENTIC),
                    "graph_id": "graph-B",
                },
            ),
        ]
        base = _FakeBaseRetriever(items=items)

        with use_organisation_context(_context(_ORG_AUTHENTIC)):
            wrapper = klasses["Org"](
                base_retriever=base,
                context=_context(_ORG_AUTHENTIC),
                graph_id="graph-A",
            )
            result = wrapper.get_search_results(query_text="hi")

        assert [i.content for i in result.items] == ["kept"], (
            "AC3 end-to-end: the substring-attack item leaked through the org-scoped post-filter."
        )
