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
    (Neo4j elementId), and ``type`` is the relationship type.  Edge-level data
    (e.g. the ``score`` written onto SIMILAR_TO/SAME_AS_CANDIDATE by the
    resolver) lives inside ``properties`` — mirroring :class:`NodeResult`.
    """

    source: str
    target: str
    type: str
    properties: dict[str, Any]


class SubgraphResult(TypedDict):
    """A bounded slice of a graph for visualisation: a capped set of nodes plus
    the edges that fall entirely within that set (org+graph scoped)."""

    nodes: list[NodeResult]
    edges: list[EdgeResult]


class FederatedNodeResult(TypedDict):
    """A :class:`NodeResult` labeled with the graph it came from (#330 / ADR-026).

    Every federated hit carries ``source_graph_id`` + ``source_graph_name`` at the top level so a
    caller aggregating many graphs can always attribute a result to its origin."""

    id: str
    type: str
    properties: dict[str, Any]
    source_graph_id: str
    source_graph_name: str


class FederatedEdgeResult(TypedDict):
    """An :class:`EdgeResult` labeled with its source graph (#330). Edges in a federated
    neighborhood always have both endpoints inside ONE graph — federation never fabricates
    cross-graph edges on the read path (cross-graph SAME_AS is the KGS HITL pipeline's job)."""

    source: str
    target: str
    type: str
    properties: dict[str, Any]
    source_graph_id: str
    source_graph_name: str
