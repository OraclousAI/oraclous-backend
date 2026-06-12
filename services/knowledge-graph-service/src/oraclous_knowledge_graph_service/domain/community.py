"""Community-detection domain layer (ORAA-4 §21 domain layer — pure, no I/O) (#303).

Restores the legacy hierarchical community capability
(``knowledge-graph-builder/app/tasks/community_tasks.py`` +
``services/community_summarizer.py`` + ``analytics_service.py``), RE-ARCHITECTED onto in-DB Neo4j
GDS Louvain: the in-memory ``leidenalg``/``igraph`` pipeline is gone — community detection now runs
as Cypher ``CALL gds.louvain.*`` inside Neo4j (the repositories layer). Leiden is Enterprise-only
so it is deliberately NOT used; Louvain is the Community-Edition algorithm.

This module holds only the pure pieces: the deterministic community-id scheme (matched to the legacy
16-char SHA-256 contract), the resolution sweep that synthesises the 5-level hierarchy, value
objects, and the typed errors. No driver, no Cypher, no LLM — those live in
``repositories.community_repository`` / ``services.analytics_service`` /
``services.community_summarizer``.

The 5-level multi-resolution hierarchy on Community GDS Louvain
==============================================================
GDS Louvain on Community Edition has NO ``resolution``/``gamma`` parameter (that knob is
Leiden/Enterprise). The multi-level hierarchy is instead synthesised by running Louvain at five
weight-contrast exponents over the SAME projected subgraph: each edge weight ``w`` is projected as
``w ** resolution``. Raising the exponent sharpens the contrast between strong and weak edges, so
Louvain's modularity objective favours finer/smaller communities; lowering it flattens the contrast
and yields coarser/larger communities. A live probe on ``neo4j:5.23-community`` (GDS 2.11) confirmed
the sweep is monotonic — a planted 4-super × 3-sub hierarchy resolves to 4 communities at the lowest
exponent up to 12 at the highest — so the exponent reproduces the legacy gamma-sweep semantics
(higher resolution → more, smaller communities) using only the Community-tier algorithm.

``level`` indexes the sweep (0 = coarsest resolution, 4 = finest), matching the legacy
``DEFAULT_LEVELS`` ordering; ``resolution`` is the exponent applied at that level.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

# The 5-level multi-resolution sweep (ORAA #303). Ascending resolution → finer communities. Index in
# this tuple is the community `level`; the value is the weight-contrast exponent passed to Louvain.
DEFAULT_RESOLUTIONS: tuple[float, ...] = (0.5, 1.0, 2.0, 3.0, 4.0)
DEFAULT_LEVELS: tuple[int, ...] = tuple(range(len(DEFAULT_RESOLUTIONS)))

# A graph smaller than this many entities is not worth detecting communities over (mirrors the
# legacy COMMUNITY_DETECTION_MIN_ENTITIES default). The detect use-case treats it as a skip, not an
# error.
DEFAULT_MIN_ENTITIES: int = 3

# Community node / membership-edge labels — the legacy unified-model contract (kept verbatim so the
# explorer and any legacy reader see the same shapes).
COMMUNITY_LABEL = "__Community__"
ENTITY_LABEL = "__Entity__"
IN_COMMUNITY_REL = "IN_COMMUNITY"
PARENT_COMMUNITY_REL = "PARENT_COMMUNITY"

# The only community kind this service detects (entity-level Louvain). The legacy registry also
# carried a read-only chunk kind; that is out of scope for the restoration and is reported as a
# non-detectable kind by the discovery endpoint if ever added.
ENTITY_KIND = "entity"


class CommunityDetectionError(Exception):
    """Base for detection failures that map to a clean HTTP error (not a swallowed 500)."""


class GdsUnavailableError(CommunityDetectionError):
    """The Neo4j GDS procedures are not installed/loadable (``gds.*`` missing).

    Raised by the repository when a ``gds.louvain``/``gds.graph.project`` call fails because the
    plugin is absent, so the service maps it to a clear 503 ("community detection unavailable: the
    Neo4j Graph Data Science plugin is not loaded") rather than a generic 500. Fail-closed and
    typed — the failure mode is observable, never silently swallowed.
    """


def make_community_id(
    *, graph_id: str, level: int, resolution: float, member_ids: list[str]
) -> str:
    """Deterministic ``community_<16-hex>`` id (legacy 16-char SHA-256 scheme, verbatim).

    The id is a hash over the graph, level, resolution and the SORTED member ids, so the same set of
    members at the same level/resolution always yields the same id (idempotent re-detection MERGEs
    onto the existing node) and a different membership yields a different id. Sorting makes the id
    order-independent.
    """
    sorted_ids = sorted(member_ids)
    content = f"{graph_id}|L{level}|R{resolution}|" + "|".join(sorted_ids)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return f"community_{digest}"


@dataclass(frozen=True)
class CommunityMember:
    """One entity belonging to a community (for the detail view)."""

    entity_id: str
    entity_name: str
    entity_type: str


@dataclass(frozen=True)
class Community:
    """A detected community node, as read back from Neo4j (the list/detail view shape).

    ``summary``/``summary_keywords``/``summary_excerpt`` are populated by the summarizer pass (None
    until summarised). ``parent_id`` links a finer-level community to its coarser-level parent
    (majority-vote of shared members), reproducing the legacy PARENT_COMMUNITY hierarchy.
    """

    community_id: str
    kind: str
    level: int
    resolution: float
    entity_count: int
    status: str
    weight: float | None = None
    parent_id: str | None = None
    summary: str | None = None
    summary_keywords: list[str] | None = None
    summary_excerpt: str | None = None
    summary_model: str | None = None
    summary_at: datetime | None = None
    members: list[CommunityMember] = field(default_factory=list)


@dataclass(frozen=True)
class DetectionResult:
    """The outcome of a detection run (returned to the caller / job result)."""

    graph_id: str
    status: str  # "completed" | "skipped"
    total_communities: int
    communities_per_level: dict[int, int]
    entities_processed: int
    reason: str | None = None


@dataclass(frozen=True)
class CommunitiesStatus:
    """Detection status for a graph (mirrors the legacy ``/communities/status`` shape).

    Derived live from the graph's ``:__Community__`` nodes + current entity count — the new build
    no Postgres ``communities_status`` column, so status is read from the substrate (Source of truth
    is the graph itself): ``not_detected`` when no community nodes exist, else ``active``, plus a
    staleness signal from the entity delta since detection.
    """

    graph_id: str
    status: str  # "not_detected" | "active"
    communities_count: int
    levels: list[int]
    entity_count: int
    entity_count_at_detection: int
    is_stale: bool


@dataclass(frozen=True)
class GraphAnalytics:
    """Graph-level summary statistics (mirrors the legacy ``/analytics`` shape).

    Entity/relationship counts, label + relationship-type breakdowns, density, average degree, and
    the top entities by degree — all org+graph scoped, read live from Neo4j.
    """

    graph_id: str
    node_count: int
    relationship_count: int
    entity_count: int
    density: float
    avg_degree: float
    entity_types: list[dict[str, object]]
    relationship_types: list[dict[str, object]]
    top_entities: list[dict[str, object]]
    community_count: int
    computed_at: datetime


@dataclass(frozen=True)
class CommunityKind:
    """A community kind the platform can surface (the discovery endpoint shape)."""

    kind: str
    display_name: str
    community_label: str
    member_label: str
    hierarchical: bool
    detection_supported: bool


def entity_kinds() -> list[CommunityKind]:
    """The community-kind registry. Today the only kind is entity-level Louvain (hierarchical,
    detectable). Kept as a function (not a const) so a future read-only kind can be appended without
    a breaking change to callers."""
    return [
        CommunityKind(
            kind=ENTITY_KIND,
            display_name="Entity communities",
            community_label=COMMUNITY_LABEL,
            member_label=ENTITY_LABEL,
            hierarchical=True,
            detection_supported=True,
        )
    ]


def build_parent_links(
    levels_membership: dict[int, dict[str, list[str]]],
) -> dict[int, dict[str, str | None]]:
    """Assign each community a parent in the next-coarser level by majority vote of shared members.

    ``levels_membership`` maps level → {community_id: [entity_id, ...]}. For every level except the
    coarsest (level 0), each community's parent is the coarser-level community that owns the most of
    its members (the legacy ``_build_hierarchy`` majority vote). Pure — no I/O. Returns level →
    {community_id: parent_id|None}.
    """
    ordered_levels = sorted(levels_membership)
    # entity -> community at each level
    entity_to_comm: dict[int, dict[str, str]] = {}
    for level in ordered_levels:
        lookup: dict[str, str] = {}
        for cid, members in levels_membership[level].items():
            for eid in members:
                lookup[eid] = cid
        entity_to_comm[level] = lookup

    parents: dict[int, dict[str, str | None]] = {}
    for i, level in enumerate(ordered_levels):
        parents[level] = {}
        if i == 0:
            for cid in levels_membership[level]:
                parents[level][cid] = None
            continue
        coarser = ordered_levels[i - 1]
        coarser_lookup = entity_to_comm[coarser]
        for cid, members in levels_membership[level].items():
            votes: dict[str, int] = {}
            for eid in members:
                parent_cid = coarser_lookup.get(eid)
                if parent_cid:
                    votes[parent_cid] = votes.get(parent_cid, 0) + 1
            parents[level][cid] = max(votes, key=votes.__getitem__) if votes else None
    return parents
