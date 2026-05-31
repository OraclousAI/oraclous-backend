"""Multi-tenant retriever wrappers for the knowledge retriever service
(ORA-18 / Epic A3).

Lifts the three multi-tenant retriever wrappers from the legacy
``knowledge-graph-builder/app/components/multi_tenant_components.py`` and
extends each with an outer organisation-scoping layer
(``OrganisationScoped*Retriever``).

Module placement: ``solution-architect`` ratified Option B (split) on
31 May 2026 — the retrievers live here in ``knowledge-retriever-service``
(read path); the writer wrapper lives in
``oraclous_knowledge_graph_service.multi_tenant`` (write path). Both consume
the substrate seam ``oraclous_substrate.access`` per ADR-012 §1; neither forks
org-scoping.

Threats: T1 (cross-tenant retrieval). The organisation-id scope is injected
into ``filters`` + ``query_params`` (so neo4j-graphrag's index lookups honour
it), into the Cypher WHERE clause (for the ``VectorCypherRetriever`` path,
via ``oraclous_substrate.access.org_scoped_cypher`` — the ADR-012 §1 seam),
and into the tenant-specific index name. A post-filter back-stop drops any
item whose ``metadata.organisation_id`` does not match the bound context,
fail-closed on indeterminate.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

# Module-level neo4j-graphrag imports so tests can monkeypatch each base
# retriever class (the legacy composition pattern; ORA-18 Lift step preserved).
from neo4j_graphrag.retrievers import (
    HybridRetriever,
    VectorCypherRetriever,
    VectorRetriever,
)
from neo4j_graphrag.types import RetrieverResult

# Module-level (NOT a ``from-import``) so the substrate's canonical helpers
# (``ORGANISATION_ID_PROPERTY``, ``org_scope_predicate``) are reached via
# attribute lookup at use-time — this is what makes them a single source of
# truth (ADR-012 §1b / ORA-52). A ``from oraclous_substrate.access import
# ORGANISATION_ID_PROPERTY`` would capture the string at import time and
# silently break the contract.
from oraclous_substrate import access

if TYPE_CHECKING:
    from neo4j import Driver
    from neo4j_graphrag.embeddings.base import Embedder
    from neo4j_graphrag.retrievers.base import Retriever
    from oraclous_governance import OrganisationContext

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Legacy lift: MultiTenant* wrappers (graph_id only)
# --------------------------------------------------------------------------- #


class MultiTenantRetriever:
    """Base multi-tenant wrapper composing any neo4j-graphrag retriever
    (Lift step). Injects ``graph_id`` into ``filters`` + ``query_params``
    before delegating to the base; post-filters items whose metadata does
    not match.

    Duck-types as a neo4j-graphrag ``Retriever`` (exposes ``driver``,
    ``get_search_results``) without extending the base class — the base's
    ``__init__`` invokes ``driver_config.override_user_agent`` which assumes
    a real connected driver and breaks against test doubles.
    """

    def __init__(self, base_retriever: Retriever, graph_id: str) -> None:
        self.base_retriever = base_retriever
        self.graph_id = graph_id
        # Expose ``driver`` so callers that expect a Retriever-like surface
        # can still reach it through the wrapper.
        self.driver = base_retriever.driver

    def _inject_runtime_scope(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Inject ``graph_id`` into ``filters`` and ``query_params``.

        Subclasses override to add their own scope keys (organisation_id).
        """
        filters = kwargs.get("filters") or {}
        filters["graph_id"] = self.graph_id
        kwargs["filters"] = filters
        query_params = kwargs.get("query_params") or {}
        query_params["graph_id"] = self.graph_id
        kwargs["query_params"] = query_params
        return kwargs

    def _keep_item(self, item: object) -> bool:
        """Post-filter back-stop: drop any item whose metadata does not match
        ``graph_id``. Defence-in-depth against a base retriever that does not
        honour the index filter.

        Strict metadata equality only (ORA-52 / AC3): a missing ``metadata``
        attribute, a missing ``graph_id`` key, or a mismatched value all drop
        the item. The legacy ``graph_id``-substring-in-``content`` fallback is
        gone — it trusted LLM-generated text to decide tenancy, which an
        attacker who controls document content can exploit to cause
        cross-graph retrieval (security-architect hygiene finding on the
        ORA-18 Code Review).
        """
        if not hasattr(item, "metadata"):
            return False
        return item.metadata.get("graph_id") == self.graph_id

    def get_search_results(
        self,
        query_vector: list[float] | None = None,
        query_text: str | None = None,
        **kwargs: Any,
    ) -> RetrieverResult:
        kwargs = self._inject_runtime_scope(kwargs)
        logger.debug("Multi-tenant search for graph %s", self.graph_id)

        result = self.base_retriever.get_search_results(
            query_vector=query_vector, query_text=query_text, **kwargs
        )
        filtered = [item for item in result.items if self._keep_item(item)]
        # Mirror the input result type so test fakes (which use plain
        # dataclasses) and the real RetrieverResult both round-trip cleanly.
        return type(result)(items=filtered)

    def __getattr__(self, name: str) -> object:
        """Delegate other attributes to the wrapped base retriever."""
        return getattr(self.base_retriever, name)


def _tenant_index_name(index_name: str, graph_id: str) -> str:
    return f"{index_name}_{graph_id}"


def _inject_graph_id_filter(query: str) -> str:
    """Inject a parameterised ``WHERE node.graph_id = $graph_id`` after the
    first ``MATCH``. Idempotent: a query already naming ``$graph_id`` is
    returned unchanged. The value travels only as the bound parameter — never
    interpolated into the query text (T1 injection safety).
    """
    if "MATCH" not in query or "$graph_id" in query:
        return query
    lines = query.split("\n")
    modified: list[str] = []
    filter_added = False
    for line in lines:
        modified.append(line)
        if line.strip().startswith("MATCH") and not filter_added:
            modified.append("WHERE node.graph_id = $graph_id")
            filter_added = True
    return "\n".join(modified)


class MultiTenantVectorRetriever(MultiTenantRetriever):
    """Multi-tenant vector retriever factory."""

    @classmethod
    def create(
        cls,
        driver: Driver,
        index_name: str,
        embedder: Embedder,
        graph_id: str,
        return_properties: list[str] | None = None,
        **kwargs: Any,
    ) -> MultiTenantVectorRetriever:
        base_retriever = VectorRetriever(
            driver=driver,
            index_name=_tenant_index_name(index_name, graph_id),
            embedder=embedder,
            return_properties=return_properties or ["text", "chunk_index"],
            **kwargs,
        )
        return cls(base_retriever, graph_id)


class MultiTenantVectorCypherRetriever(MultiTenantRetriever):
    """Multi-tenant vector+cypher retriever factory."""

    @classmethod
    def create(
        cls,
        driver: Driver,
        index_name: str,
        embedder: Embedder,
        retrieval_query: str,
        graph_id: str,
        **kwargs: Any,
    ) -> MultiTenantVectorCypherRetriever:
        safe_query = _inject_graph_id_filter(retrieval_query)
        base_retriever = VectorCypherRetriever(
            driver=driver,
            index_name=_tenant_index_name(index_name, graph_id),
            embedder=embedder,
            retrieval_query=safe_query,
            **kwargs,
        )
        return cls(base_retriever, graph_id)

    @staticmethod
    def _inject_graph_id_filter(query: str) -> str:
        """Public-ish helper kept for backward compatibility with the legacy
        symbol name."""
        return _inject_graph_id_filter(query)


class MultiTenantHybridRetriever(MultiTenantRetriever):
    """Multi-tenant hybrid (vector + fulltext) retriever factory."""

    @classmethod
    def create(
        cls,
        driver: Driver,
        vector_index_name: str,
        fulltext_index_name: str,
        embedder: Embedder,
        graph_id: str,
        **kwargs: Any,
    ) -> MultiTenantHybridRetriever:
        base_retriever = HybridRetriever(
            driver=driver,
            vector_index_name=_tenant_index_name(vector_index_name, graph_id),
            fulltext_index_name=_tenant_index_name(fulltext_index_name, graph_id),
            embedder=embedder,
            **kwargs,
        )
        return cls(base_retriever, graph_id)


# --------------------------------------------------------------------------- #
# Outer organisation-scoping layer (Extend step). Consumes ADR-012 §1.
# --------------------------------------------------------------------------- #


def _tenant_index_name_org(index_name: str, organisation_id: str, graph_id: str) -> str:
    """Tenant index name including ``organisation_id`` so two organisations
    sharing the same ``graph_id`` cannot read each other's vectors."""
    return f"{index_name}_{organisation_id}_{graph_id}"


_TERMINATOR_CLAUSE = re.compile(r"\s+(RETURN|WITH|ORDER\s+BY|LIMIT|SKIP)\b", re.IGNORECASE)


def _build_scoped_query(query: str) -> str:
    """Compose the org-scoped Cypher used at retrieval time.

    Returns a query carrying both ``$organisation_id`` and ``$graph_id``
    parameter placeholders (never interpolates either value). Idempotent.

    The WHERE clause is spliced *before* the first terminator clause
    (RETURN / WITH / ORDER BY / LIMIT / SKIP) so the resulting Cypher is
    valid for single-line ``MATCH ... RETURN ...`` templates as well as
    multi-line ones (a naive after-MATCH splice produces
    ``RETURN ...\\nWHERE ...`` which Neo4j rejects).

    Sources the ``organisation_id`` predicate fragment from the substrate
    seam via ``access.org_scope_predicate`` rather than re-deriving it
    inline (ADR-012 §1b / ORA-52 AC1 — single source of truth).
    """
    if "$graph_id" in query and "$organisation_id" in query:
        return query
    if "MATCH" not in query.upper():
        # Nothing to anchor the scope to — refuse rather than emit unscoped.
        raise ValueError("build_scoped_query: no MATCH clause to anchor org scope")
    # Compose the org predicate from the substrate (single source of truth).
    # The graph_id half stays local — it's a retriever-internal scope, not a
    # substrate concept.
    org_predicate = access.org_scope_predicate(alias="node")
    predicate = f"node.graph_id = $graph_id AND {org_predicate}"
    # If a WHERE already exists, AND-extend the first one.
    where_match = re.search(r"\bWHERE\b", query, re.IGNORECASE)
    if where_match is not None:
        terminator = _TERMINATOR_CLAUSE.search(query, where_match.end())
        cut = terminator.start() if terminator is not None else len(query)
        return f"{query[:cut].rstrip()} AND {predicate}{query[cut:]}"
    # Otherwise splice ``WHERE <predicate>`` before the first terminator.
    terminator = _TERMINATOR_CLAUSE.search(query)
    if terminator is None:
        # Bare ``MATCH ...`` with nothing after — append WHERE at the end.
        return f"{query.rstrip()}\nWHERE {predicate}"
    return f"{query[: terminator.start()]}\nWHERE {predicate}{query[terminator.start() :]}"


class OrganisationScopedRetriever(MultiTenantRetriever):
    """Outer organisation-scoping wrapper above ``MultiTenantRetriever``
    (Extend step).

    Fail-closed at runtime: ``organisation_id`` is sourced live from the
    authenticated bound context via
    ``oraclous_substrate.access.enforced_organisation_id`` — never from a
    constructor argument or a request body (ADR-012 §1b / T1-M1 / ORA-52
    AC2). Injects ``organisation_id`` into ``filters`` + ``query_params``
    alongside the legacy ``graph_id``; post-filters items whose metadata does
    not match (drops cross-org items AND items with missing
    ``organisation_id`` — fail-closed on indeterminate).

    The ``context`` keyword argument is accepted for backward compatibility
    with the original ORA-18 call sites but is **deliberately ignored** —
    the substrate seam is the single source of truth and a
    request-body-supplied ``organisation_id`` cannot redirect scope at
    construction (T1-M1).
    """

    def __init__(
        self,
        base_retriever: Retriever,
        graph_id: str,
        context: OrganisationContext | None = None,  # noqa: ARG002 — deprecated; ignored
    ) -> None:
        super().__init__(base_retriever, graph_id)

    @property
    def _organisation_id(self) -> str:
        """Read live from the substrate seam — never captured at construction
        and never sourced from a constructor argument (AC2 / T1-M1).
        Raises ``MissingOrganisationContextError`` when no context is bound.
        """
        return access.enforced_organisation_id()

    def _inject_runtime_scope(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        # Legacy graph_id injection first
        kwargs = super()._inject_runtime_scope(kwargs)
        # Then unconditionally overwrite organisation_id (T1: caller cannot
        # widen scope via filters/query_params). The key comes from the
        # substrate's canonical constant (AC1 — single source of truth);
        # the value comes from the bound seam (AC2 — authenticated context).
        org_id = self._organisation_id  # raises if no bound context (live)
        kwargs["filters"][access.ORGANISATION_ID_PROPERTY] = org_id
        kwargs["query_params"][access.ORGANISATION_ID_PROPERTY] = org_id
        return kwargs

    def _keep_item(self, item: object) -> bool:
        # First the legacy graph_id check (defence-in-depth back-stop)
        if not super()._keep_item(item):
            return False
        # Then strict organisation_id check — fail-closed on missing metadata.
        # Key from the substrate (AC1); value from the bound seam (AC2).
        if not hasattr(item, "metadata"):
            return False
        return item.metadata.get(access.ORGANISATION_ID_PROPERTY) == self._organisation_id


class OrganisationScopedVectorRetriever(OrganisationScopedRetriever):
    """Outer organisation-scoping vector retriever factory.

    Fail-closed at construction: ``.create()`` resolves ``organisation_id``
    via the substrate seam (``access.enforced_organisation_id``) so the
    tenant index name carries the *authenticated* organisation — a
    request-body-supplied ``organisation_id`` cannot redirect index naming
    (AC2 / T1-M1). The ``context`` kwarg is accepted for backward
    compatibility but is ignored (ORA-52).
    """

    @classmethod
    def create(
        cls,
        driver: Driver,
        index_name: str,
        embedder: Embedder,
        graph_id: str,
        *,
        context: OrganisationContext | None = None,  # noqa: ARG003 — deprecated; ignored
        return_properties: list[str] | None = None,
        **kwargs: Any,
    ) -> OrganisationScopedVectorRetriever:
        org_id = access.enforced_organisation_id()  # fail-closed at construction (AC2)
        base_retriever = VectorRetriever(
            driver=driver,
            index_name=_tenant_index_name_org(index_name, org_id, graph_id),
            embedder=embedder,
            return_properties=return_properties or ["text", "chunk_index"],
            **kwargs,
        )
        return cls(base_retriever, graph_id)


class OrganisationScopedVectorCypherRetriever(OrganisationScopedRetriever):
    """Outer organisation-scoping vector+cypher retriever factory.

    The Cypher template gets both ``$organisation_id`` and ``$graph_id`` WHERE
    predicates spliced in via the substrate's canonical
    ``access.org_scope_predicate`` (AC1 — single source of truth),
    parameterised — never interpolated. Construction fail-closes on the
    bound seam (AC2 / T1-M1).
    """

    @classmethod
    def create(
        cls,
        driver: Driver,
        index_name: str,
        embedder: Embedder,
        retrieval_query: str,
        graph_id: str,
        *,
        context: OrganisationContext | None = None,  # noqa: ARG003 — deprecated; ignored
        **kwargs: Any,
    ) -> OrganisationScopedVectorCypherRetriever:
        org_id = access.enforced_organisation_id()  # fail-closed at construction (AC2)
        scoped_query = _build_scoped_query(retrieval_query)
        base_retriever = VectorCypherRetriever(
            driver=driver,
            index_name=_tenant_index_name_org(index_name, org_id, graph_id),
            embedder=embedder,
            retrieval_query=scoped_query,
            **kwargs,
        )
        return cls(base_retriever, graph_id)

    @staticmethod
    def build_scoped_query(query: str) -> str:
        """Public helper exposing the scoped-Cypher composer used at
        retrieval time. Used by the organisation_isolation integration
        gate to drive scoped reads against a real Neo4j."""
        return _build_scoped_query(query)


class OrganisationScopedHybridRetriever(OrganisationScopedRetriever):
    """Outer organisation-scoping hybrid (vector + fulltext) retriever factory.

    Construction fail-closes on the bound seam (AC2 / T1-M1); the ``context``
    kwarg is accepted but ignored (ORA-52).
    """

    @classmethod
    def create(
        cls,
        driver: Driver,
        vector_index_name: str,
        fulltext_index_name: str,
        embedder: Embedder,
        graph_id: str,
        *,
        context: OrganisationContext | None = None,  # noqa: ARG003 — deprecated; ignored
        **kwargs: Any,
    ) -> OrganisationScopedHybridRetriever:
        org_id = access.enforced_organisation_id()  # fail-closed at construction (AC2)
        base_retriever = HybridRetriever(
            driver=driver,
            vector_index_name=_tenant_index_name_org(vector_index_name, org_id, graph_id),
            fulltext_index_name=_tenant_index_name_org(fulltext_index_name, org_id, graph_id),
            embedder=embedder,
            **kwargs,
        )
        return cls(base_retriever, graph_id)
