"""Public read-path contracts for knowledge-retriever-service (ORAA-60).

NodeResult is the canonical OHM-shaped envelope returned by all five
retrieval endpoints.  It mirrors the shape defined in
``oraclous_knowledge_graph_service.contracts`` but is re-declared here so
that KRS does not import from a sibling service (import-linter §layers
prohibits same-layer cross-imports — KRS and KGS live at the same tier).
"""

from __future__ import annotations

from typing import Any, TypedDict


class NodeResult(TypedDict):
    """OHM envelope for a retrieved knowledge-graph node.

    Returned by all five read-path endpoints.  Modality-specific fields
    (scores, vectors, text hits, traversal depth, temporal bounds) are
    carried inside ``properties`` — never at the top level of the response.
    """

    id: str
    type: str
    properties: dict[str, Any]


class EdgeResult(TypedDict):
    """A directed relationship between two nodes in a returned subgraph.

    ``source``/``target`` are the ``id`` of the corresponding :class:`NodeResult`
    (Neo4j elementId), and ``type`` is the relationship type.
    """

    source: str
    target: str
    type: str


class SubgraphResult(TypedDict):
    """A bounded slice of a graph for visualisation: a capped set of nodes plus
    the edges that fall entirely within that set (org+graph scoped)."""

    nodes: list[NodeResult]
    edges: list[EdgeResult]
